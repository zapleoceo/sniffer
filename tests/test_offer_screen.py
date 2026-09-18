"""ИИ-проверка карточек: мусор гаснет, товар уточняется. Модель замокана.

Тексты — живые карточки прода 18.09.2026, прошедшие бесплатный гейт: обмен
валют стал квартирой, распродажа списком — машиной, VinFast EVO отвечал на
запрос «байк от 200 кубов».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sniffer.broker.client import BrokerCapError, BrokerError
from sniffer.domain.records import Listing
from sniffer.verifier.offer_screen import (
    OfferVerdict,
    parse_verdicts,
    screen_offers,
    screen_prompt,
    screen_schema,
)
from sniffer.worker.screening import BATCHES_PER_TICK, Screening, screened_fields


def listing(listing_id: int, category: str, title: str, summary: str = "") -> Listing:
    return Listing(
        id=listing_id,
        raw_message_id=None,
        deal_type="sell",
        category=category,
        city="nha_trang",
        title=title,
        summary=summary or title,
        tg_link=f"https://t.me/c/1/{listing_id}",
        posted_at=datetime(2026, 9, 18, tzinfo=UTC),
    )


def row(n: int, kind: str = "offer", category: str = "motorbike", **extra: str) -> dict[str, Any]:
    values = {
        "n": n,
        "kind": kind,
        "category": category,
        "deal": "sell",
        "power": "unknown",
        "brand": "",
        "engine_cc": "",
        "rooms": "",
        "why": "",
    }
    return {**values, **extra}


class Broker:
    def __init__(self, *answers: dict[str, Any] | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def structured(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_schema_is_closed_and_prompt_numbers_every_post() -> None:
    item = screen_schema()["properties"]["verdicts"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    prompt = screen_prompt(["Обмен валют", "Продам Honda Lead"])
    assert "### 1\nОбмен валют" in prompt and "### 2\nПродам Honda Lead" in prompt


def test_verdicts_keep_only_closed_values_and_plausible_numbers() -> None:
    payload = {
        "verdicts": [
            row(1, power="electric", brand="VinFast", engine_cc="5000"),
            row(2, kind="maybe"),
            row(7),
            row(3, category="apartment", rooms="2"),
        ]
    }

    first, second, third = parse_verdicts(payload, 3)

    assert first == OfferVerdict("offer", "motorbike", "sell", "electric", brand="vinfast")
    assert second is None, "вне закрытого списка — вердикта нет"
    assert third is not None and third.rooms == 2


def test_junk_goes_out_of_the_catalog() -> None:
    exchange = listing(1, "apartment", "#Обмен", "Безопасный обмен валют, выдача наличных по QR")
    junk = OfferVerdict("junk", "other", "sell", "unknown", why="обмен")
    fields = screened_fields(exchange, junk)
    assert fields == {"keep": False, "note": "junk/other: обмен"}

    sale = listing(2, "car", "Продаю", "1. Аудиосистема 2. Тату машинка")
    assert not screened_fields(sale, OfferVerdict("offer", "other", "sell", "unknown"))["keep"]
    wanted = listing(3, "motorbike", "Ищу байк в аренду")
    assert not screened_fields(wanted, OfferVerdict("wanted", "motorbike", "sell", "unknown"))[
        "keep"
    ]


def test_an_offer_gets_power_and_only_fills_gaps() -> None:
    evo = listing(4, "motorbike", "Сдам VinFast EVO", "Сдам VinFast EVO 200, 150к в сутки")
    evo.attributes.update({"brand": "vinfast", "engine_cc": 200})
    verdict = OfferVerdict(
        "offer", "motorbike", "rent_out", "electric", brand="honda", engine_cc=50
    )

    fields = screened_fields(evo, verdict)

    assert fields["keep"] and fields["deal_type"] == "rent_out"
    assert fields["attributes"] == {"brand": "vinfast", "engine_cc": 200, "power": "electric"}


def test_a_new_category_rereads_attributes_for_it() -> None:
    text = "Сдаётся квартира, автомат-стиралка, 2 спальни"
    flat = listing(5, "motorbike", "Сдаётся квартира", text)
    flat.attributes.update({"transmission": "automatic", "power": "fuel"})

    fields = screened_fields(flat, OfferVerdict("offer", "apartment", "rent_out", "unknown"))

    assert fields["category"] == "apartment"
    assert "transmission" not in fields["attributes"] and "power" not in fields["attributes"]
    assert fields["attributes"]["rooms"] == 2


async def test_screen_offers_maps_verdicts_by_number() -> None:
    broker = Broker({"verdicts": [row(2, kind="junk", category="other"), row(1)]})

    first, second = await screen_offers(["Продам Lead", "Обмен валют"], broker)

    assert first is not None and first.is_offer
    assert second is not None and not second.is_offer
    assert await screen_offers([], broker) == []


class Catalog:
    def __init__(self, rows: list[Listing]) -> None:
        self.rows = rows
        self.applied: dict[int, dict[str, Any]] = {}

    async def unscreened(self, limit: int) -> list[Listing]:
        left = [r for r in self.rows if r.id not in self.applied]
        return left[:limit]

    async def apply(self, listing_id: int, fields: dict[str, Any]) -> None:
        self.applied[listing_id] = fields


def job(broker: Broker, catalog: Catalog, now: list[float]) -> Screening:
    return Screening(
        broker=broker, unscreened=catalog.unscreened, apply=catalog.apply, clock=lambda: now[0]
    )


async def test_the_job_reads_batches_and_marks_skipped_numbers_read() -> None:
    catalog = Catalog([listing(1, "motorbike", "Lead"), listing(2, "apartment", "Обмен")])
    broker = Broker({"verdicts": [row(2, kind="junk", category="other")]})

    assert await job(broker, catalog, [0.0]).tick() == 2

    assert catalog.applied[1] == {"keep": True, "note": "no_verdict"}
    assert catalog.applied[2]["keep"] is False
    assert len(broker.prompts) == 1, "вторая пачка пуста — модель не зовём"


async def test_a_broker_failure_pauses_and_keeps_the_cards() -> None:
    catalog = Catalog([listing(1, "motorbike", "Lead")])
    now = [0.0]
    screening = job(Broker(BrokerError("down"), {"verdicts": [row(1)]}), catalog, now)

    assert await screening.tick() == 0
    assert catalog.applied == {}
    assert await screening.tick() == 0, "пауза — модель не зовём"
    now[0] += 601
    assert await screening.tick() == 1


async def test_the_daily_cap_stops_until_midnight() -> None:
    catalog = Catalog([listing(1, "motorbike", "Lead")])
    now = [0.0]
    screening = job(Broker(BrokerCapError("cap"), {"verdicts": [row(1)]}), catalog, now)

    await screening.tick()
    now[0] += 601
    assert await screening.tick() == 0, "лимит до полуночи UTC, а не на десять минут"
    now[0] += 86_400
    assert await screening.tick() == 1


async def test_a_tick_is_bounded_so_other_steps_run() -> None:
    catalog = Catalog([listing(n, "motorbike", f"Lead {n}") for n in range(1, 100)])
    answers = [{"verdicts": [row(i) for i in range(1, 16)]} for _ in range(5)]

    await job(Broker(*answers), catalog, [0.0]).tick()

    assert len(catalog.applied) == 15 * BATCHES_PER_TICK


async def test_without_a_broker_key_the_job_sleeps(monkeypatch: Any) -> None:
    from sniffer.config import get_settings

    monkeypatch.setattr(get_settings(), "broker_project_key", "")
    catalog = Catalog([listing(1, "motorbike", "Lead")])

    assert await Screening(unscreened=catalog.unscreened, apply=catalog.apply).tick() == 0
