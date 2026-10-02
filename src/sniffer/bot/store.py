"""Состояние диалога живёт в базе, а не в памяти процесса.

Бот перезапускается на каждом деплое, а уточняющий вопрос висит между двумя
сообщениями клиента. Держи мы «о чём спросили» в словаре процесса — каждый
деплой стирал бы начатые разговоры, и клиент получал бы «Что ищем?» второй раз
подряд.

FSM aiogram здесь не используется намеренно: его хранилище пришлось бы писать
поверх той же таблицы, а состояние всё равно обязано лежать в
`passport_events` — паспорт ведёт эту историю и без диалога. Два хранилища
одного и того же расходятся, одно — нет.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.domain.dialogue import EVENT_USER_MESSAGE, DialogueState, advance, replay
from sniffer.domain.passport import Passport
from sniffer.domain.records import QueryOverview, StoredPassport

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class Client:
    """Кто пишет. Всё, что боту нужно знать о человеке до записи в базу."""

    tg_user_id: int
    username: str | None = None


@dataclass(frozen=True, slots=True)
class Dialogue:
    """Текущий разговор: чей, о чём и на каком вопросе остановились.

    «О чём» — это всегда ОДНА ветка (поиск), активная. Паспорта соседних веток
    сюда не попадают вовсе, и это не упрощение, а граница: пока в разговоре лежит
    один паспорт, ни одна эвристика не может увести уточнение в чужую ветку —
    сравнивать ей просто не с чем.
    """

    user_id: int
    passport: StoredPassport | None = None
    state: DialogueState = field(default_factory=DialogueState)
    editing: bool = False
    # Человек сказал `/new` и ещё не написал, что ищет. Пока флаг взведён,
    # следующее сообщение открывает ветку, а не уточняет активную. Это СНИМОК на
    # момент `load`: настоящий флаг в базе, и тратится он там же (`start_requested`).
    starting_new: bool = False


class DialogueStore(Protocol):
    """Чем диалог обменивается с хранилищем.

    Протокол, а не класс: тесты подставляют словарь и проверяют ход диалога
    без Postgres, а живой бот — реальные таблицы.
    """

    async def load(self, client: Client) -> Dialogue: ...

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue: ...

    async def start_requested(self, dialogue: Dialogue, passport: Passport) -> Dialogue | None:
        """Новая ветка по `/new`: флаг тратится В ТОМ ЖЕ действии, что и создание.

        `None` — флаг уже потрачен другим сообщением, и ветка ему не нужна. Именно
        это отличает метод от `start`: тот создаёт безусловно.
        """
        ...

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue: ...

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue: ...

    async def select(self, dialogue: Dialogue, root: int, *, editing: bool = False) -> Dialogue: ...

    async def live_threads(self, dialogue: Dialogue) -> list[QueryOverview]: ...

    async def await_new(self, dialogue: Dialogue) -> None: ...


class PassportStore:
    """`DialogueStore` поверх таблиц `passports` и `passport_events`."""

    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def load(self, client: Client) -> Dialogue:
        async with self._sessions() as session:
            user = await UserRepository(session).get_or_create(
                client.tg_user_id, username=client.username
            )
            await session.commit()
            if user.id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
                raise LookupError(f"клиент {client.tg_user_id} без id")

            passports = PassportRepository(session)
            current = await passports.get_current(user.id)
            if current is None:
                return Dialogue(user_id=user.id)
            events = await passports.list_events(current.root)
            return Dialogue(
                user_id=user.id,
                passport=current,
                state=replay(events),
                editing=user.editing_passport_root == current.root,
                starting_new=user.awaiting_new_request,
            )

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue:
        """Новая формулировка — новая цепочка версий и чистый счётчик вопросов."""
        async with self._sessions() as session:
            stored = await self._insert(session, dialogue.user_id, passport)
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def start_requested(self, dialogue: Dialogue, passport: Passport) -> Dialogue | None:
        """`/new`: потратить флаг и создать ветку одной транзакцией.

        Порядок «сначала флаг, потом ветка» и общая транзакция — это и есть
        гарантия. Флаг, снятый ДО разбора, терялся бы при упавшем разборе; флаг,
        снятый отдельной транзакцией после создания, оставлял бы окно, в котором
        два сообщения открывают по ветке. Здесь проигравший не вставляет ничего:
        сессия закрывается без коммита.
        """
        async with self._sessions() as session:
            if not await PassportRepository(session).consume_new_request(dialogue.user_id):
                return None
            stored = await self._insert(session, dialogue.user_id, passport)
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def _insert(
        self, session: AsyncSession, user_id: int, passport: Passport
    ) -> StoredPassport:
        passports = PassportRepository(session)
        stored = await passports.save_new(user_id, passport)
        await passports.add_event(stored.id, EVENT_USER_MESSAGE, {"text": passport.raw_query})
        await session.commit()
        return stored

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue:
        """Правка поля — новая версия, а не перезапись (passport.md)."""
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("нечего уточнять: паспорта ещё нет")
        async with self._sessions() as session:
            passports = PassportRepository(session)
            stored = await passports.save_revision(dialogue.passport, passport)
            await passports.add_event(stored.id, kind, payload)
            await session.commit()
        return Dialogue(
            user_id=dialogue.user_id,
            passport=stored,
            state=advance(dialogue.state, kind, payload),
        )

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue:
        """Событие без правки паспорта: заданный вопрос или пропуск «не важно»."""
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("событие без паспорта")
        async with self._sessions() as session:
            await PassportRepository(session).add_event(dialogue.passport.id, kind, payload)
            await session.commit()
        return replace(dialogue, state=advance(dialogue.state, kind, payload))

    async def live_threads(self, dialogue: Dialogue) -> list[QueryOverview]:
        """Ветки в списке, недавно использованные сверху. Нужны, чтобы знать, есть ли место."""
        async with self._sessions() as session:
            return await PassportRepository(session).list_queries(dialogue.user_id)

    async def await_new(self, dialogue: Dialogue) -> None:
        """`/new`: следующее сообщение открывает ветку, а не уточняет активную."""
        async with self._sessions() as session:
            await PassportRepository(session).await_new_request(dialogue.user_id)
            await session.commit()

    async def select(self, dialogue: Dialogue, root: int, *, editing: bool = False) -> Dialogue:
        """Переключить контекст только на принадлежащую клиенту цепочку."""
        async with self._sessions() as session:
            passports = PassportRepository(session)
            if not await passports.select(dialogue.user_id, root, editing=editing):
                return dialogue
            current = await passports.get_current(dialogue.user_id)
            events = [] if current is None else await passports.list_events(current.root)
            await session.commit()
        return Dialogue(
            user_id=dialogue.user_id,
            passport=current,
            state=replay(events),
            editing=editing,
        )
