"""Маппинг строка ↔ домен. Без базы: ORM-объект собирается в памяти."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sniffer.db import models
from sniffer.db.mappers import (
    passport_values,
    raw_message_values,
    to_chat,
    to_job,
    to_listing,
    to_stored_passport,
    to_user,
)
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport, PassportStatus
from sniffer.domain.records import RawMessage, StoredPassport

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_chat_row_to_domain() -> None:
    chat = to_chat(
        models.Chat(
            id=7,
            tg_id=-100123,
            username="nhatrang_bikes",
            title="Нячанг байки",
            city="nha_trang",
            categories=["motorbike"],
            is_active=True,
            search_rank=10,
            msg_count_24h=42,
            last_msg_id=555,
            last_synced_at=NOW,
            added_at=NOW,
        )
    )
    assert (chat.id, chat.tg_id, chat.last_msg_id) == (7, -100123, 555)
    assert chat.categories == ["motorbike"]


def test_listing_keeps_decimal_price() -> None:
    """Цена не должна проходить через float: 7 000 000 ₫ дороже округления."""
    listing = to_listing(
        models.Listing(
            id=1,
            raw_message_id=2,
            deal_type="rent_out",
            category="apartment",
            city="nha_trang",
            title="Студия у моря",
            summary="1 комната, 40 м²",
            tg_link="https://t.me/c/1/2",
            posted_at=NOW,
            price_amount=Decimal("7000000.00"),
            price_currency="VND",
            price_period="month",
            price_usd_month=Decimal("280.00"),
            attributes={"rooms": 1},
            confidence=0.8,
            extracted_at=NOW,
            is_active=True,
        )
    )
    assert listing.price_amount == Decimal("7000000.00")
    assert listing.attributes == {"rooms": 1}
    assert listing.district is None


def test_user_row_to_domain() -> None:
    user = to_user(models.User(id=3, tg_user_id=99, username="dima", lang="ru", is_blocked=False))
    assert (user.id, user.tg_user_id, user.lang) == (3, 99, "ru")


def test_job_row_to_domain() -> None:
    job = to_job(
        models.Job(id=5, kind="extract", payload={"raw_id": 12}, attempts=1, run_after=NOW)
    )
    assert (job.id, job.kind, job.attempts) == (5, "extract", 1)
    assert job.payload == {"raw_id": 12}


def test_raw_message_values_have_no_id() -> None:
    """BIGSERIAL выдаёт id сам; подставленный вручную сломал бы батч-вставку."""
    values = raw_message_values(
        RawMessage(
            id=999,
            chat_tg_id=-100,
            msg_id=1,
            text="продам байк 7 триеу",
            text_hash="abc",
            posted_at=NOW,
        )
    )
    assert "id" not in values
    assert values["chat_tg_id"] == -100
    assert values["stage"] == "pending"


def _passport() -> Passport:
    return Passport(
        intent=Intent.RENT,
        category=Category.APARTMENT,
        city="nha_trang",
        districts=["Vinh Hai"],
        budget=Budget(min=200, max=400, currency=Currency.USD),
        attributes={"rooms": 1, "furnished": True},
        must_have=["air_conditioner"],
        deal_breakers=["ground_floor"],
        timeframe_from=date(2026, 9, 1),
        raw_query="студия у моря до 400",
        confidence=0.7,
        missing_fields=["attributes.rooms"],
        status=PassportStatus.READY,
    )


def test_passport_values_are_json_ready() -> None:
    """В JSONB нельзя положить Currency.USD, а в TEXT-колонку — Intent.RENT."""
    values = passport_values(_passport())
    assert values["intent"] == "rent"
    assert values["category"] == "apartment"
    assert values["status"] == "ready"
    assert values["budget"] == {"min": 200.0, "max": 400.0, "currency": "USD", "period": "month"}
    assert values["timeframe_from"] == date(2026, 9, 1)


def test_passport_survives_round_trip() -> None:
    original = _passport()
    row = models.Passport(id=1, user_id=2, version=1, root_id=None, **passport_values(original))
    row.is_current = True
    restored = to_stored_passport(row)

    assert restored.passport == original
    assert restored.root == 1, "у первой версии корнем служит её собственный id"


def test_revision_keeps_root() -> None:
    row = models.Passport(id=9, user_id=2, version=2, root_id=1, **passport_values(_passport()))
    row.is_current = True
    assert to_stored_passport(row).root == 1


def test_empty_budget_column_gives_default_budget() -> None:
    """Старые строки писались до появления поля — падать на них нельзя."""
    stored = StoredPassport(
        id=1,
        user_id=1,
        version=1,
        passport=to_stored_passport(
            models.Passport(
                id=1,
                user_id=1,
                version=1,
                status="draft",
                districts=[],
                budget={},
                attributes={},
                must_have=[],
                deal_breakers=[],
                raw_query="",
                confidence=0.0,
                missing_fields=[],
            )
        ).passport,
    )
    assert stored.passport.budget == Budget()
