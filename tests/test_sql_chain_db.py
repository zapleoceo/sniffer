"""Повторный деплой на живых данных ничего не меняет (живой Postgres).

`test_sql_chain.py` ловит ТЕКСТ правки данных, здесь проверяется ПОВЕДЕНИЕ:
цепочка применяется так же, как это делает `infra/deploy.sh` (каждый файл по
алфавиту), поверх состояния, которое бывает только у работающей системы, и
после второго и третьего прогона ни одна строка не должна измениться.

Ловит то, чего текст не покажет: правку, спрятанную в `DO $$ ... $$`, в
`INSERT ... SELECT` или в триггере, и сид, который возвращает в очередь тех, кого
оттуда уже вывели. Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from sniffer.db import collection_models, models

# Таблицы сборщика регистрируются в метаданных только импортом этого модуля, а
# фикстура `db_engine` чистит ВСЕ таблицы метаданных. В полном прогоне модуль
# подгружают соседние тесты, поэтому по одному файлу тест без явного импорта
# падал бы на `NoReferencedTableError`.
assert collection_models

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

SQL_DIR = Path(__file__).parents[1] / "infra" / "sql"
# Только метка времени для посева строк: с `now()` базы тест её не сверяет, но
# правило `test_db_clock_rule` запрещает зашитую дату во всех тестах с живой базой.
NOW = datetime.now(UTC)

# Дайджест таблицы целиком: строка как текст, в стабильном порядке. Берётся
# сырым SQL сознательно: нужна не модель, а ВСЁ, что лежит в строке, включая
# колонки, о которых тест не знает. Переименованная таблица уронит запрос
# громко, а не пропустит проверку молча.
DIGESTS = {
    "listings": "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '') FROM listings t",
    "chats": "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '') FROM chats t",
    "chat_candidates": (
        "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.key)), '') FROM chat_candidates t"
    ),
    "chat_rejects": (
        "SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.key)), '') FROM chat_rejects t"
    ),
}

# Сид-чаты, которых очередь уже выпустила тремя разными путями.
JOINED, REFUSED, EXHAUSTED = "@auto_moto_vietnam", "@nyachang_uslugi", "@nha_trang_kupi_proday"


async def deploy(engine: AsyncEngine) -> None:
    """Как `infra/deploy.sh`: каждый файл цепочки по алфавиту, один за другим."""
    async with engine.connect() as conn:
        driver = (await conn.get_raw_connection()).driver_connection
        assert driver is not None
        for path in sorted(SQL_DIR.glob("00*.sql")):
            await driver.execute(path.read_text(encoding="utf-8"))


async def digests(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        return {
            table: (await conn.execute(text(sql))).scalar_one() for table, sql in DIGESTS.items()
        }


async def queued(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set((await conn.execute(select(models.ChatCandidate.key))).scalars())


def live_state() -> list[models.Base]:
    """Состояние, которое есть у работающей системы и которого нет у чистой базы."""
    return [
        # Сторону сделки назвала ИИ-проверка («Продажи» не из тех слов, что ищет
        # регулярка), и пересмотреть её уже некому: `screened_at` заполнен.
        models.Listing(
            source="telegram_archive",
            external_id="chain:ai-sell",
            deal_type="sell",
            category="apartment",
            city="nha_trang",
            title="Продажи квартир",
            summary="Продажи квартир у моря, всего от 2 млрд VND",
            price_amount=Decimal("2000000000"),
            price_currency="VND",
            price_period="once",
            tg_link="https://t.me/x/1",
            posted_at=NOW,
            screened_at=NOW,
            screen_note="offer/apartment: sale",
        ),
        models.Listing(
            source="telegram_archive",
            external_id="chain:rent",
            deal_type="rent_out",
            category="apartment",
            city="nha_trang",
            title="Сдаю студию",
            summary="Сдаю студию у моря, 8 млн в месяц",
            price_amount=Decimal("8000000"),
            price_currency="VND",
            price_period="month",
            tg_link="https://t.me/x/2",
            posted_at=NOW,
        ),
        # Вступили: кандидата из очереди убрали, чат стоит в реестре.
        models.Chat(tg_id=-1001, username="auto_moto_vietnam", title="чат", city="nha_trang"),
        # Отказали и исчерпали попытки: кандидата нет, в журнале отказов есть.
        models.ChatReject(key=REFUSED, reason="join_refused"),
        models.ChatReject(key=EXHAUSTED, reason="too_many_attempts"),
    ]


async def test_a_fresh_database_gets_the_whole_seed(db_engine: AsyncEngine) -> None:
    await deploy(db_engine)

    seed = (SQL_DIR / "002_seed_candidates.sql").read_text(encoding="utf-8")
    expected = len(re.findall(r"\('@[^']+', '[^']+', 'seed:", seed))
    async with db_engine.connect() as conn:
        got = (
            await conn.execute(
                select(func.count()).where(models.ChatCandidate.found_in.like("seed:%"))
            )
        ).scalar_one()

    assert got == expected > 0


async def test_repeated_deploys_change_nothing_on_live_data(db_engine: AsyncEngine) -> None:
    await deploy(db_engine)
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            delete(models.ChatCandidate).where(
                models.ChatCandidate.key.in_([JOINED, REFUSED, EXHAUSTED])
            )
        )
        session.add_all(live_state())
    before, queue_before = await digests(db_engine), await queued(db_engine)

    await deploy(db_engine)
    await deploy(db_engine)
    after, queue_after = await digests(db_engine), await queued(db_engine)

    assert queue_after - queue_before == set(), (
        f"деплой вернул в очередь тех, кого оттуда уже вывели: {sorted(queue_after - queue_before)}"
    )
    changed = sorted(table for table in DIGESTS if before[table] != after[table])
    assert not changed, f"повторный деплой изменил данные в таблицах: {changed}"
