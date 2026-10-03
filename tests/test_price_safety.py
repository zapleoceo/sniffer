"""Разбор цены не падает и не стоит секунды на любом тексте, какой пришлют чаты.

Воронка обрабатывает сообщения по одному: исключение из разбора цены роняет
обработку архива на этом сообщении и на всех после него. Поэтому здесь не примеры
«как надо», а свойство: что бы ни стояло в тексте, ответ — цена или `None`.
"""

from __future__ import annotations

import random
import time

import pytest

from sniffer.domain.price_numbers import expand_compact
from sniffer.domain.prices import parse_price, parse_prices, price_hint

APARTMENT = {"category": "apartment", "deal_type": "rent_out"}

# Любой пробельный знак, а не только пробел: регулярка числа пускает `\s` после
# точки-разделителя групп, и разбор числа обязан знать их все. Прежняя чистка
# знала только пробел, и «1.<таб>000.000» ронял воронку на `float`.
ODD_SPACES = {
    "tab": "\t",
    "ogham_space": chr(0x1680),
    "unit_separator": chr(0x1F),
    "em_space": chr(0x2003),
    "ideographic_space": chr(0x3000),
    "no_break_space": chr(0xA0),
    "narrow_no_break_space": chr(0x202F),
}


@pytest.mark.parametrize("space", ODD_SPACES.values(), ids=ODD_SPACES.keys())
def test_a_group_separator_of_any_whitespace_is_read_not_a_crash(space: str) -> None:
    text = f"Цена: 12.{space}500.000 VND/месяц"

    assert [fact.amount for fact in parse_prices(text)] == [12_500_000]
    fact = parse_price(text, **APARTMENT)
    assert fact is not None
    assert fact.amount == 12_500_000
    assert price_hint(text)[1] == 12_500_000


def test_a_compact_amount_is_expanded_in_linear_time() -> None:
    """Без левой границы серия цифр пробовалась с каждой позиции: 6000 цифр — 272 мс."""
    started = time.perf_counter()

    assert expand_compact("9" * 60_000) == "9" * 60_000
    assert expand_compact("9" * 50_000 + "tr5") == "9" * 50_000 + "tr5"

    assert time.perf_counter() - started < 1.0


def test_a_compact_amount_is_still_expanded_after_a_text() -> None:
    assert expand_compact("Giá 9tr5/tháng, 16tr50") == "Giá 9.5 tr/tháng, 16.5 tr"
    assert expand_compact("1.9tr5") == "1.9.5 tr"


# Знаки конца строки (вертикальная табуляция, разделитель строк Юникода) тоже здесь:
# `splitlines` режет по ним, и число, разорванное таким знаком, обязано остаться
# безобидным.
_RARE_WHITESPACE = [chr(code) for code in (0x09, 0x0B, 0x0C, 0x1C, 0x1F, 0x85, 0xA0, 0x1680)] + [
    chr(code) for code in (0x2003, 0x2028, 0x202F, 0x3000)
]
_PIECES = [
    *(
        "0 1 2 3 5 7 9 12 100 250 3000 9500 19.500 7,500,000 15.000. 000 1 000 000 "
        "млн тыс к кк tr tỷ triệu m ml k vnd ₫ d $ usd дол. руб eur "
        "цена аренда стоимость оплата price rent giá залог депозит "
        "месяц мес сутки ночь неделя год month day "
        "до от to - – — / : , . ; ! ? ( ) [ ] # + ~ ≈ "
        "💰 💵 🍋 1️⃣ 𝟙𝟚 № м2 м² этаж спален комнат"
    ).split(" "),
    *_RARE_WHITESPACE,
]


def _random_texts(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)  # noqa: S311 -- не криптография: воспроизводимый перебор
    texts = []
    for _ in range(count):
        words = [rng.choice(_PIECES) for _ in range(rng.randint(1, 40))]
        joiner = rng.choice([" ", "", "\n", " \n"])
        texts.append(joiner.join(words))
    return texts


@pytest.mark.parametrize("seed", range(4))
def test_no_random_mix_of_price_words_and_digits_crashes_the_parser(seed: int) -> None:
    """Свойство, а не пример: список ожидаемых ошибок — и есть то, что забывают."""
    for text in _random_texts(800, seed):
        for kind in ({}, APARTMENT, {"category": "motorbike", "deal_type": "sell"}):
            parse_price(text, **kind)
        price_hint(text)
