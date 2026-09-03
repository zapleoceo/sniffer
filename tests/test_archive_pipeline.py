from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.domain.prices import MAX_PLAUSIBLE_VND, price_hint
from sniffer.domain.records import Chat, RawMessage
from sniffer.pipeline.archive import STAGE_REJECTED, classify, listing_from
from sniffer.search import vocabulary
from sniffer.search.intake_rules import parse_query

# Боевой путь: воркер внедряет детектор категорий из словаря поиска, поэтому
# лот, названный одной маркой или моделью, доходит до карточки. Гейт сам про них
# больше не знает.
DETECTOR = vocabulary.category_hints


@pytest.fixture
def chat() -> Chat:
    return Chat(tg_id=-1001234567890, username="nha_trang_flea", title="Flea", city="nha_trang")


def raw(text: str, *, identifier: int = 7) -> RawMessage:
    return RawMessage(
        id=identifier,
        chat_tg_id=-1001234567890,
        msg_id=88,
        text=text,
        text_hash="hash",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def test_passing_archive_message_becomes_linked_listing(chat: Chat) -> None:
    message = raw("Продам Yamaha Nouvo, цена — 3.7tr, документы есть")

    listing = listing_from(message, chat, classify(message, category_hints=DETECTOR))

    assert listing.category == "motorbike"
    assert listing.city == "nha_trang"
    assert listing.price_amount == 3_700_000
    assert listing.tg_link == "https://t.me/nha_trang_flea/88"


def test_explicit_attributes_survive_the_archive_pipeline(chat: Chat) -> None:
    message = raw("Продам Honda Lead 125, автомат, 12 млн VND")
    parsed = parse_query(message.text, default_city=chat.city)
    listing = listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        deal_type="sell",
        attributes=dict(parsed.attributes),
    )

    assert listing.deal_type == "sell"
    assert listing.attributes["brand"] == "honda"
    assert listing.attributes["model"] == "lead"
    assert listing.attributes["transmission"] == "automatic"


@pytest.mark.parametrize(
    "text",
    ["Ищу скутер до 5 млн, подскажите", "Honda Vision 2021, состояние отличное"],
)
def test_demand_and_chatter_never_become_listings(text: str) -> None:
    result = classify(raw(text), category_hints=DETECTOR)

    assert not result.passed
    assert result.reason in {"demand_not_offer", "no_price_no_offer_verb"}
    assert STAGE_REJECTED == "rejected"


def test_private_chat_link_uses_internal_telegram_id() -> None:
    message = raw("Продам Honda Vision, цена 22 млн")
    chat = Chat(tg_id=-1001234567890, title="Private", city="nha_trang")

    listing = listing_from(message, chat, classify(message, category_hints=DETECTOR))

    assert listing.tg_link == "https://t.me/c/1234567890/88"


# ── правдоподобие цены ──────────────────────────────────────────────────────


def test_a_price_multiplied_into_the_trillions_is_no_price_at_all() -> None:
    """Живой отказ 01.09.2026, из-за которого встала вся воронка.

    Продавцы пишут «21.500.000 млн VND», имея в виду 21.5 млн. Умножение даёт
    21.5 триллиона, вставка падает на `NUMERIC(14,2)`, воркер уходит в цикл
    перезапуска. Обрезать до потолка нельзя: обрезанное число выглядит как
    настоящая цена и попадёт в фильтр по бюджету, то есть соврёт тише.
    """
    assert price_hint("Цена: 21.500.000 млн VND") == ("", None)


def test_a_normal_price_still_reads() -> None:
    """Порог не смеет отсекать настоящие цены нячангского рынка."""
    assert price_hint("Цена: 21.5 млн VND")[1] == 21_500_000
    assert price_hint("Цена: 15.000.000 VND")[1] == 15_000_000
    assert price_hint("Цена 22 мил")[1] == 22_000_000


def test_the_ceiling_is_where_the_column_ends() -> None:
    """Потолок правдоподобия и потолок колонки — одна и та же граница."""
    assert MAX_PLAUSIBLE_VND == 10_000_000_000
    assert price_hint(f"Цена: {MAX_PLAUSIBLE_VND} VND")[1] == MAX_PLAUSIBLE_VND
    assert price_hint(f"Цена: {MAX_PLAUSIBLE_VND + 1} VND") == ("", None)


def test_a_lot_names_its_own_city_and_the_card_believes_the_lot(chat: Chat) -> None:
    """Город лота главнее города чата: иначе ханойская квартира станет нячангской.

    Пока в реестре были только нячангские группы, разница не проявлялась. Первая
    общевьетнамская барахолка её вскрывает: матчер фильтрует ровно по этому полю
    (`matching/rules.py`), значит город чата дошёл бы до выдачи как правда.
    """
    message = raw("Продам Honda Vision в Дананге, 15 млн VND")
    parsed = parse_query(message.text, default_city=chat.city)

    listing = listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        attributes=dict(parsed.attributes),
        city=parsed.city or "",
    )

    assert listing.city == "da_nang", "карточка поверила чату, а не объявлению"


def test_a_lot_without_a_city_falls_back_to_the_chat(chat: Chat) -> None:
    """Молчание объявления о городе — не «города нет», а «спроси у чата».

    Иначе большинство карточек осталось бы вовсе без города: в нячангской группе
    город в тексте не пишут, он и так понятен из места.
    """
    message = raw("Продам Honda Vision, 15 млн VND")
    parsed = parse_query(message.text, default_city=chat.city)

    listing = listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        city=parsed.city or "",
    )

    assert listing.city == "nha_trang"


def test_a_multi_city_chat_does_not_stamp_its_own_city_on_everything() -> None:
    """Общевьетнамская барахолка: город лота решает по каждому объявлению.

    Ровно тот чат, из-за которого правка и появилась (`@vietavito`, «все
    барахолки»): чат говорит «здесь торгуют», а не «здесь торгуют в Нячанге».
    """
    aggregator = Chat(
        tg_id=-1003986519874,
        username="vietavito",
        title="Vietavito | все барахолки",
        city="nha_trang",
    )
    lots = {
        "Сдам квартиру в Ханое, 8 млн донгов": "ha_noi",
        "Продам скутер, Фукуок, 12 млн": "phu_quoc",
        "Продам Honda Lead, Нячанг, 14 млн": "nha_trang",
        "Продам Honda Lead, 14 млн": "nha_trang",
    }

    for text, expected in lots.items():
        message = raw(text)
        parsed = parse_query(text, default_city=aggregator.city)
        listing = listing_from(
            message,
            aggregator,
            classify(message, category_hints=DETECTOR),
            city=parsed.city or "",
        )
        assert listing.city == expected, f"{text!r} → {listing.city}, ждали {expected}"
