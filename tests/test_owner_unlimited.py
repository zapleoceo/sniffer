"""Владелец без пределов: слежение без слота, платный предел поисков, безлимит карточек.

Пределы клиента считаются от покупок, а владелец бота у самого себя ничего не покупает:
без исключения он проверял бы живую систему одним поиском и без единого слежения — то
есть мимо той половины бота, ради которой всё и писалось.

Исключение ОДНО и названо в `Settings.is_owner`; здесь проверяется, что каждый из
пределов спрашивает именно его, и что незаданный владелец (`OWNER_CHAT_ID=0`) не раздаёт
безлимит никому.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot import watch_flow
from sniffer.bot.billing_slots import DbSlots
from sniffer.bot.store import Client
from sniffer.bot.watch_panel import PanelView, limit_text
from sniffer.config import Settings, reload_settings
from sniffer.db import models
from sniffer.db.repositories.passports import PassportRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.plans import FREE_SEARCHES, PAID_SEARCHES
from sniffer.domain.slots import Outcome
from tests.subscription_support import grant

OWNER_TG = 169510539
STRANGER_TG = 111222333
NOW = datetime.now(UTC).replace(microsecond=0)

needs_db = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


@pytest.fixture
def owner_is(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Владелец задан в окружении так же, как в боевом контейнере."""
    monkeypatch.setenv("OWNER_CHAT_ID", str(OWNER_TG))
    reload_settings()
    yield
    # Настройки кэшируются на процесс: не сбросив кэш, следующий тест увидел бы владельца.
    reload_settings()


def test_an_unset_owner_makes_nobody_unlimited() -> None:
    """Ноль — это «владельца нет», а не «владелец с id 0»: безлимита не получает никто."""
    nobody = Settings(_env_file=None, owner_chat_id=0)  # type: ignore[call-arg]
    assert nobody.owner_tg_id is None
    assert not nobody.is_owner(0)
    assert not nobody.is_owner(OWNER_TG)

    set_up = Settings(_env_file=None, owner_chat_id=OWNER_TG)  # type: ignore[call-arg]
    assert set_up.is_owner(OWNER_TG)
    assert not set_up.is_owner(STRANGER_TG)


def test_the_upgrade_is_offered_only_when_it_raises_the_cap() -> None:
    """Владельцу с платным пределом без покупки подписку не предлагают: она ничего не даст."""
    free = PanelView([], paid_slots=0, bound_slots=0, used=FREE_SEARCHES, cap=FREE_SEARCHES)
    granted = PanelView([], paid_slots=0, bound_slots=0, used=PAID_SEARCHES, cap=PAID_SEARCHES)
    assert "подписку" in limit_text(free)
    assert "подписку" not in limit_text(granted)


@asynccontextmanager
async def _one(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Одна живая сессия вместо `session_scope`: коммиты настоящие, база настоящая."""
    yield session


async def _client(session: AsyncSession, tg_id: int) -> int:
    user = await UserRepository(session).get_or_create(tg_id, username="тест")
    assert user.id is not None
    await session.commit()
    return user.id


async def _branch(session: AsyncSession, user_id: int) -> int:
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    stored = await PassportRepository(session).save_new(user_id, passport)
    await session.commit()
    return stored.root


async def _monitors(session: AsyncSession, user_id: int) -> list[models.Subscription]:
    rows = await session.execute(
        select(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .order_by(models.Subscription.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_the_owner_follows_any_search_without_paying_for_a_slot(
    db_session: AsyncSession,
) -> None:
    """«Следить» у владельца включается без платежа — и сразу на нескольких поисках."""
    user_id = await _client(db_session, OWNER_TG)
    first, second = await _branch(db_session, user_id), await _branch(db_session, user_id)
    slots = DbSlots(sessions=lambda: _one(db_session))

    assert await slots.enable(OWNER_TG, first, NOW) is Outcome.ENABLE
    assert await slots.enable(OWNER_TG, second, NOW) is Outcome.ENABLE

    rows = await _monitors(db_session, user_id)
    assert [row.passport_root for row in rows] == [first, second]
    assert [row.expires_at for row in rows] == [None, None], "выдача без платежа — без срока"
    assert all(row.is_active for row in rows)


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_the_owner_pressing_follow_twice_is_told_it_is_already_on(
    db_session: AsyncSession,
) -> None:
    """Повтор ничего не заводит заново: один мониторинг на ветку, как и у платящего."""
    user_id = await _client(db_session, OWNER_TG)
    root = await _branch(db_session, user_id)
    slots = DbSlots(sessions=lambda: _one(db_session))

    assert await slots.enable(OWNER_TG, root, NOW) is Outcome.ENABLE
    assert await slots.enable(OWNER_TG, root, NOW) is Outcome.ALREADY_ON
    assert len(await _monitors(db_session, user_id)) == 1


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_follow_on_a_paid_branch_keeps_its_paid_period(db_session: AsyncSession) -> None:
    """Нажатие на оплаченной ветке не обнуляет срок: иначе кнопка отдавала бы чужой слот.

    Ветка владельца, но срок у неё оплаченный — так выглядит мониторинг, получивший слот
    от платежа. Исход тот же, что у клиента: «уже включено», строка не тронута.
    """
    user_id = await _client(db_session, OWNER_TG)
    root = await _branch(db_session, user_id)
    paid_until = NOW + timedelta(days=30)
    await grant(db_session, user_id, root, until=paid_until)
    await db_session.commit()

    slots = DbSlots(sessions=lambda: _one(db_session))
    assert await slots.enable(OWNER_TG, root, NOW) is Outcome.ALREADY_ON
    assert [row.expires_at for row in await _monitors(db_session, user_id)] == [paid_until]


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_a_paused_monitor_of_the_owner_comes_back_without_a_period(
    db_session: AsyncSession,
) -> None:
    """Пауза снимается тем же нажатием: мониторинг возвращается в работу и снова бессрочный."""
    user_id = await _client(db_session, OWNER_TG)
    root = await _branch(db_session, user_id)
    monitor_id = await grant(db_session, user_id, root, until=NOW - timedelta(days=1))
    paused = await db_session.get(models.Subscription, monitor_id)
    assert paused is not None
    paused.is_active = False
    await db_session.commit()

    slots = DbSlots(sessions=lambda: _one(db_session))
    assert await slots.enable(OWNER_TG, root, NOW) is Outcome.ENABLE
    rows = await _monitors(db_session, user_id)
    assert [(row.id, row.is_active, row.expires_at) for row in rows] == [(monitor_id, True, None)]


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_a_stranger_still_needs_a_subscription(db_session: AsyncSession) -> None:
    """Исключение одно и штучное: чужому «Следить» без слота по-прежнему не включается."""
    user_id = await _client(db_session, STRANGER_TG)
    root = await _branch(db_session, user_id)
    slots = DbSlots(sessions=lambda: _one(db_session))

    assert await slots.enable(STRANGER_TG, root, NOW) is Outcome.NEEDS_SUBSCRIPTION
    assert await _monitors(db_session, user_id) == []


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_the_owner_keeps_opening_searches_past_the_free_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Бесплатный предел — один поиск; владелец открывает их до платного, не архивируя."""
    user_id = await _client(db_session, OWNER_TG)
    for _ in range(FREE_SEARCHES + 1):
        await _branch(db_session, user_id)
    monkeypatch.setattr(watch_flow, "session_scope", lambda: _one(db_session))

    assert await watch_flow.DbSearchLimit().blocked(user_id) is None

    stranger_id = await _client(db_session, STRANGER_TG)
    for _ in range(FREE_SEARCHES + 1):
        await _branch(db_session, stranger_id)
    refusal = await watch_flow.DbSearchLimit().blocked(stranger_id)
    assert refusal is not None and "подписку" in refusal


@needs_db
@pytest.mark.usefixtures("owner_is")
async def test_the_owner_panel_shows_the_paid_cap_without_claiming_a_purchase(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предел платный, но «куплено» остаётся нулём: панель не выдумывает покупок."""
    user_id = await _client(db_session, OWNER_TG)
    await _branch(db_session, user_id)
    monkeypatch.setattr(watch_flow, "session_scope", lambda: _one(db_session))

    view = await watch_flow.panel(Client(OWNER_TG, "zapleosoft"))
    assert view is not None
    assert (view.cap, view.paid_slots) == (PAID_SEARCHES, 0)
