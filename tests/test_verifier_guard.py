"""Охранник выдачи: что он вправе сделать с карточкой, а что нет."""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.broker.client import BrokerError
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.sources.base import RawItem
from sniffer.verifier.guard import screen

WANTED = Passport(
    intent=Intent.BUY,
    category=Category.MOTORBIKE,
    city="nha_trang",
    budget=Budget(max=10_000_000, currency=Currency.VND),
)


def item(number: int, text: str, **overrides: Any) -> RawItem:
    fields: dict[str, Any] = {
        "source": "telegram_groups",
        "external_id": str(number),
        "url": f"https://t.me/c/1/{number}",
        "text": text,
    }
    fields.update(overrides)
    return RawItem(**fields)


class FakeBroker:
    """Брокер без сети. Отдаёт заготовленный вердикт или падает."""

    def __init__(
        self, payload: dict[str, Any] | None = None, boom: Exception | None = None
    ) -> None:
        self.payload = payload or {"verdicts": []}
        self.boom = boom
        self.calls = 0
        self.prompts: list[str] = []

    async def structured(self, prompt: str, **_: Any) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        if self.boom is not None:
            raise self.boom
        return self.payload

    async def aclose(self) -> None:
        return None


def verdict(number: int, **fields: Any) -> dict[str, Any]:
    row = {"n": number, "keep": True, "why": "", "price_vnd": "", "price_text": ""}
    row.update(fields)
    return row


async def test_a_rejected_card_never_reaches_the_client() -> None:
    """Ради этого охранник и заведён: колонка JBL в ответ на «мотоцикл»."""
    items = [item(1, "Продам Honda Vision"), item(2, "Loa Bluetooth JBL Charge 5")]
    broker = FakeBroker({"verdicts": [verdict(1), verdict(2, keep=False, why="это колонка")]})

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert [card.external_id for card in kept] == ["1"]


async def test_a_price_written_without_a_label_is_recovered() -> None:
    """«Штормовая скидка! 40 миллионов» — регексп такое не берёт, модель берёт."""
    items = [item(1, "Штормовая скидка! 40 миллионов) Кастомный байк")]
    broker = FakeBroker({"verdicts": [verdict(1, price_vnd="40000000", price_text="40 миллионов")]})

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd == 40_000_000
    assert card.price_raw == "40 миллионов", "клиенту показываем слова продавца"


async def test_an_invented_price_is_thrown_away_not_shown() -> None:
    """Придуманная цена не смеет стать утверждением перед клиентом.

    Фрагмент, которого в объявлении нет, — выдумка модели. Карточку оставляем:
    она могла быть верной и без цены; цену — выбрасываем целиком.
    """
    items = [item(1, "Продам байк, звоните")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="5000000", price_text="Цена: 5.000.000 VND")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd is None and card.price_raw == ""


async def test_our_own_price_is_never_overwritten() -> None:
    """Регексп взял цену по явной метке — это надёжнее догадки модели."""
    items = [item(1, "Цена: 7.000.000 VND", price_vnd=7_000_000, price_raw="Цена: 7.000.000 VND")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="9000000", price_text="Цена: 7.000.000 VND")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd == 7_000_000


async def test_an_implausible_number_is_not_a_price() -> None:
    """Десять миллиардов донгов — это ошибка чтения, а не байк в Нячанге."""
    items = [item(1, "Продам байк 99999999999999")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="99999999999999", price_text="99999999999999")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd is None


async def test_a_card_the_model_said_nothing_about_survives() -> None:
    """Молчание — не отказ: непроверенная карточка лучше потерянной."""
    items = [item(1, "Продам байк"), item(2, "Продам скутер")]
    broker = FakeBroker({"verdicts": [verdict(1)]})

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert [card.external_id for card in kept] == ["1", "2"]


@pytest.mark.parametrize("boom", [BrokerError("брокер лёг"), TimeoutError(), OSError("сеть")])
async def test_a_broken_guard_does_not_cancel_the_answer(boom: Exception) -> None:
    """Охранник — улучшение, а не условие работы."""
    items = [item(1, "Продам байк"), item(2, "Продам скутер")]
    broker = FakeBroker(boom=boom)

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert len(kept) == 2


async def test_only_the_head_of_the_list_costs_money() -> None:
    """Клиенту показываются единицы карточек — проверять сотню незачем."""
    items = [item(number, "Продам байк") for number in range(1, 31)]
    broker = FakeBroker({"verdicts": []})

    kept = await screen(WANTED, items, broker=broker, limit=5)  # type: ignore[arg-type]

    assert broker.calls == 1
    assert len(kept) == 30, "непроверенный хвост не выбрасывается"
    assert broker.prompts[0].count(") Продам байк") == 5


async def test_nothing_to_check_costs_nothing() -> None:
    broker = FakeBroker()

    assert await screen(WANTED, [], broker=broker) == []  # type: ignore[arg-type]
    assert broker.calls == 0


async def test_the_budget_reaches_the_model_in_dongs() -> None:
    """Объявления написаны в донгах, а бюджет клиент назвал в долларах."""
    dollars = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=300, currency=Currency.USD),
    )
    broker = FakeBroker({"verdicts": []})

    await screen(dollars, [item(1, "байк")], broker=broker, usd_vnd=26000)  # type: ignore[arg-type]

    assert "7 800 000 VND" in broker.prompts[0]


async def test_without_a_rate_the_model_is_not_told_a_made_up_budget() -> None:
    dollars = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=300, currency=Currency.USD),
    )
    broker = FakeBroker({"verdicts": []})

    await screen(dollars, [item(1, "байк")], broker=broker)  # type: ignore[arg-type]

    # Слово «бюджет» есть в самой инструкции — проверяем именно потолок.
    assert "бюджет до" not in broker.prompts[0]
