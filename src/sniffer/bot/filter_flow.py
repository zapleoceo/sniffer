"""Что делает карточка фильтра с базой: открыть, поправить, начать правку текстом.

Тонкий слой между кнопками (`filter_card`) и сервисом правки (`PassportEditor`): находит
клиента, превращает исход записи в понятный результат. Принадлежность поиска проверяет
сервис по корню, а не по кнопке: корень приезжает от клиента Telegram, и доверять ему нельзя.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sniffer.bot import query_menu
from sniffer.bot.filter_card import CURSOR_NOTE, STALE, CardView
from sniffer.bot.store import Client
from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.passport_edit import PassportEditor, StaleVersion
from sniffer.domain.passport_edit import Change, EditError


@dataclass(frozen=True, slots=True)
class Outcome:
    view: CardView | None  # None — поиск не найден (чужой, убранный, несуществующий)
    note: str | None = None


async def open_card(client: Client, root: int) -> CardView | None:
    return (await _view(client, root)).view


async def edit(client: Client, root: int, base_version: int, changes: Sequence[Change]) -> Outcome:
    """Применить правку к версии, на которую смотрел человек; при расхождении — свежая карточка."""
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        if user.id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
            return Outcome(None)
        try:
            await PassportEditor(session).edit(
                user_id=user.id, root=root, base_version=base_version, changes=changes
            )
            await session.commit()
            note = None
        except LookupError:
            return Outcome(None)
        except StaleVersion:
            await session.rollback()
            note = STALE
        except EditError as exc:
            await session.rollback()
            note = str(exc)
    found = await _view(client, root)
    live = found.view is not None and found.view.monitoring in {"active", "paused"}
    if note is None and live:
        note = CURSOR_NOTE
    return Outcome(found.view, note)


async def start_text_edit(client: Client, root: int) -> bool:
    """Следующее сообщение человека правит ЭТОТ поиск: тот же путь, что у «✏️ Изменить»."""
    return await query_menu.select(client, root, editing=True)


async def _view(client: Client, root: int) -> Outcome:
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        await session.commit()
        if user.id is None:  # pragma: no cover
            return Outcome(None)
        overview = await PassportRepository(session).get_query(user.id, root)
        if overview is None:
            return Outcome(None)
        current = await PassportEditor(session).current(user_id=user.id, root=root)
    return Outcome(CardView(root, current.version, overview.passport, overview.monitoring))
