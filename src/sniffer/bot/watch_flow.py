"""Панель слежений и управление слотом: чтение, пауза, перенос, «Удалить поиск», предел поисков.

Каждая функция — своя транзакция с явным коммитом. Принадлежность поиска проверяет
репозиторий по корню: кнопка приезжает от клиента Telegram, и корню на ней веры нет.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot.billing_slots import DbSlots
from sniffer.bot.store import Client
from sniffer.bot.watch_panel import PanelView, limit_text
from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.billing import BillingRepository
from sniffer.db.repositories.slots import SlotRepository
from sniffer.db.repositories.tabs import TabRepository
from sniffer.db.repositories.watch import WatchRepository
from sniffer.domain.plans import search_cap
from sniffer.domain.records import QueryOverview

# Столько поисков показывает панель: верхний из возможных пределов (платный). Меньший
# предел бесплатного — про открытие нового поиска, а не про показ уже существующих.
PANEL_LIMIT = 10


async def _user_id(session: AsyncSession, client: Client) -> int | None:
    user = await UserRepository(session).get_or_create(client.tg_user_id, username=client.username)
    await session.commit()
    return user.id


async def panel(client: Client, *, now: datetime | None = None) -> PanelView | None:
    moment = now or datetime.now(UTC)
    async with session_scope() as session:
        user_id = await _user_id(session, client)
        if user_id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
            return None
        items = await PassportRepository(session).list_queries(user_id, limit=PANEL_LIMIT)
        paid = await BillingRepository(session).live_subscriptions(user_id, moment)
        watch = WatchRepository(session)
        slots = await watch.live_slots(user_id, moment)
        used = await watch.count_searches(user_id)
    return PanelView(items, paid, len(slots), used, search_cap(paid))


class DbSearchLimit:
    """Предел поисков для `Conversation`: текст отказа или `None`, если место есть."""

    async def blocked(self, user_id: int) -> str | None:
        moment = datetime.now(UTC)
        async with session_scope() as session:
            paid = await BillingRepository(session).live_subscriptions(user_id, moment)
            used = await WatchRepository(session).count_searches(user_id)
        if used < search_cap(paid):
            return None
        view = PanelView([], paid, 0, used, search_cap(paid))
        return limit_text(view)


async def can_open_new(client: Client) -> tuple[bool, PanelView | None]:
    """Есть ли место под новый поиск: число не убранных поисков против предела 1 / 10."""
    view = await panel(client)
    return (view is not None and view.used < view.cap), view


async def overview(client: Client, root: int) -> QueryOverview | None:
    async with session_scope() as session:
        user_id = await _user_id(session, client)
        if user_id is None:  # pragma: no cover
            return None
        return await PassportRepository(session).get_query(user_id, root)


async def move_slot(client: Client, from_root: int, to_root: int) -> bool:
    """Перенос слота — тот же путь, что у кнопок слотов Stars: порядок претензии, не смена корня."""
    return await DbSlots().move(
        client.tg_user_id, to_root=to_root, from_root=from_root, now=datetime.now(UTC)
    )


async def archive(client: Client, root: int) -> bool:
    async with session_scope() as session:
        user_id = await _user_id(session, client)
        if user_id is None:  # pragma: no cover
            return False
        done = await WatchRepository(session).archive(user_id, root)
        if done:
            # Архив — пауза, а пауза отдаёт слот: пересчёт освобождает его для другого поиска.
            await SlotRepository(session).sync(user_id, datetime.now(UTC))
        await session.commit()
    return done


async def has_open_tab(client: Client, root: int) -> bool:
    """Есть ли у поиска живая тема (состояние `open` и известный `message_thread_id`)."""
    async with session_scope() as session:
        user_id = await _user_id(session, client)
        if user_id is None:  # pragma: no cover
            return False
        known = await TabRepository(session).tab_of(user_id, root)
    return known is not None and known[0] is not None and known[2] == "open"
