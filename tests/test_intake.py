"""Разбор формулировки клиента в паспорт.

Эвристика проверяется на тех фразах, которыми люди действительно пишут в
Нячанге: две валюты, миллионы донгов, падежные окончания и год выпуска рядом с
ценой. Модель замокана — тест, который ходит в брокер, не тест.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sniffer.broker.client import BrokerCapError, BrokerError
from sniffer.config import Settings
from sniffer.domain.passport import Category, Currency, Intent, PassportStatus, PricePeriod
from sniffer.search import intake as intake_module
from sniffer.search.intake import QueryIntake, intake_schema, merge
from sniffer.search.intake_rules import parse_query

CITY = "nha_trang"


class FakeBroker:
    """Подменяет только `structured` — больше разбору от брокера не нужно."""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    async def structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return dict(self.payload) if isinstance(self.payload, dict) else self.payload


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение без ключа брокера — иначе тест зависел бы от чужого `.env`."""
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city=CITY),
    )


@pytest.mark.parametrize(
    ("text", "category", "intent", "budget_max", "currency", "period"),
    [
        (
            "ищу скутер в Нячанге до 400 долларов",
            Category.MOTORBIKE,
            Intent.BUY,
            400,
            Currency.USD,
            PricePeriod.ONCE,
        ),
        (
            "сниму квартиру в Нячанге до 10 млн донгов",
            Category.APARTMENT,
            Intent.RENT,
            10_000_000,
            Currency.VND,
            PricePeriod.MONTH,
        ),
        (
            "куплю байк до 400$",
            Category.MOTORBIKE,
            Intent.BUY,
            400,
            Currency.USD,
            PricePeriod.ONCE,
        ),
        (
            "нужна комната до 5 млн",
            Category.ROOM,
            # «Нужна» говорит, что клиент ищет, но не говорит, покупает он или
            # снимает: комнату в Нячанге снимают.
            Intent.RENT,
            5_000_000,
            Currency.VND,
            PricePeriod.MONTH,
        ),
        (
            "сниму студию до 300 usd в месяц",
            Category.APARTMENT,
            Intent.RENT,
            300,
            Currency.USD,
            PricePeriod.MONTH,
        ),
        (
            "ищу велосипед до 100 долларов",
            Category.BICYCLE,
            Intent.BUY,
            100,
            Currency.USD,
            PricePeriod.ONCE,
        ),
    ],
    ids=["usd_scooter", "vnd_flat", "dollar_sign", "millions_no_currency", "studio", "bicycle"],
)
def test_rules_read_the_query(
    text: str,
    category: Category,
    intent: Intent,
    budget_max: float,
    currency: Currency,
    period: PricePeriod,
) -> None:
    passport = parse_query(text, default_city=CITY)

    assert passport.category is category
    assert passport.intent is intent
    assert passport.budget.max == budget_max
    assert passport.budget.currency is currency
    assert passport.budget.period is period
    assert passport.city == CITY
    assert passport.raw_query == text


def test_range_becomes_both_bounds() -> None:
    passport = parse_query("ищу квартиру в аренду от 300 до 500 долларов", default_city=CITY)

    assert passport.budget.min == 300
    assert passport.budget.max == 500


def test_year_is_not_a_price() -> None:
    """«2019 года» — это год выпуска, а не бюджет в две тысячи долларов."""
    passport = parse_query("продам скутер 2019 года", default_city=CITY)

    assert passport.intent is Intent.SELL
    assert passport.budget.max is None


def test_brand_reaches_attributes() -> None:
    """Бренд оттуда попадает первым запросом в шаблонный план поиска."""
    passport = parse_query("куплю honda vision до 400$", default_city=CITY)

    assert passport.attributes["brand"] == "honda"


def test_city_from_text_wins_over_default() -> None:
    passport = parse_query("сдам квартиру в Дананге 8 млн в месяц", default_city=CITY)

    assert passport.city == "da_nang"
    assert passport.intent is Intent.RENT_OUT


def test_unclear_query_still_gives_passport() -> None:
    """Категорию не узнали — ищем словами клиента, а не отказываемся."""
    passport = parse_query("ищу холодильник", default_city=CITY)

    assert passport.category is None
    assert passport.status is PassportStatus.DRAFT
    assert "category" in passport.missing_fields
    assert passport.raw_query == "ищу холодильник"


async def test_without_broker_key_model_is_not_called(offline: None) -> None:
    broker = FakeBroker({"category": "car"})

    passport = await QueryIntake().parse("ищу скутер до 400 долларов")

    assert not broker.calls
    assert passport.category is Category.MOTORBIKE


async def test_model_answer_refines_rules(offline: None) -> None:
    broker = FakeBroker(
        {
            "intent": "buy",
            "category": "motorbike",
            "city": "da_nang",
            "budget_max": "450",
            "currency": "USD",
            "period": "once",
            "brand": "Honda",
        }
    )

    passport = await QueryIntake(broker).parse("ищу что-нибудь на двух колёсах")

    assert len(broker.calls) == 1
    assert passport.city == "da_nang"
    assert passport.budget.max == 450
    assert passport.attributes["brand"] == "honda"
    assert passport.confidence == 0.8


async def test_model_silence_does_not_erase_rules(offline: None) -> None:
    """Пустые поля модели не должны стирать то, что разобрано по словам."""
    broker = FakeBroker(
        {
            "intent": "",
            "category": "",
            "city": "",
            "budget_max": "",
            "currency": "",
            "period": "",
            "brand": "",
        }
    )

    passport = await QueryIntake(broker).parse("ищу скутер в Нячанге до 400 долларов")

    assert passport.category is Category.MOTORBIKE
    assert passport.city == CITY
    assert passport.budget.max == 400
    assert passport.budget.currency is Currency.USD


@pytest.mark.parametrize(
    "error",
    [
        BrokerError("провайдер вернул мусор"),
        BrokerCapError("daily budget cap reached"),
        httpx.ConnectError("брокер недоступен"),
        TimeoutError("не успел"),
    ],
    ids=["broker_error", "cap_reached", "connect_error", "timeout"],
)
async def test_broker_failure_falls_back_to_rules(offline: None, error: Exception) -> None:
    passport = await QueryIntake(FakeBroker(error=error)).parse("сниму комнату до 5 млн")

    assert passport.category is Category.ROOM
    assert passport.budget.max == 5_000_000


async def test_garbage_answer_falls_back_to_rules(offline: None) -> None:
    passport = await QueryIntake(FakeBroker("не объект вовсе")).parse("ищу скутер до 400$")

    assert passport.category is Category.MOTORBIKE
    assert passport.budget.max == 400


def test_schema_is_strict() -> None:
    schema = intake_schema()

    assert schema["additionalProperties"] is False
    # strict json_schema не допускает необязательных полей: «не знаю» модель
    # говорит пустой строкой.
    assert sorted(schema["required"]) == sorted(schema["properties"])
    assert "" in schema["properties"]["category"]["enum"]
    assert CITY in schema["properties"]["city"]["enum"]


def test_merge_ignores_unknown_values() -> None:
    rules = parse_query("ищу скутер до 400$", default_city=CITY)

    merged = merge(rules, {"category": "яхта", "currency": "BTC", "budget_max": "-5"})

    assert merged.category is Category.MOTORBIKE
    assert merged.budget.currency is Currency.USD
    assert merged.budget.max == 400
