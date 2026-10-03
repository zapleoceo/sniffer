"""Названия поисков: стабильные, различимые и безопасные и для кнопки, и для HTML.

Название — единственное, по чему человек узнаёт свой поиск в `/requests`, поэтому
от него требуется три вещи сразу. Стабильность: подпись не прыгает на каждой
правке («до 500»). Различимость: два разных поиска не дают одну кнопку. Безопасность:
название собирается из слов клиента, и перевод строки, нулевой пробел или «<» в нём
не должны ни ломать ряд кнопок, ни ронять сообщение.
"""

from __future__ import annotations

import re

import pytest

from sniffer.bot import threads
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import QueryOverview
from sniffer.search.intake_rules import parse_query


def passport(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=400, currency=Currency.USD),
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


# ── из чего складывается название ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (passport(attributes={"body_type": "tay_ga"}), "Скутер, Нячанг"),
        (passport(), "Мотобайк, Нячанг"),
        (passport(intent=None), "Мотобайк, Нячанг"),
        (passport(category=Category.APARTMENT, intent=Intent.RENT), "Квартира, Нячанг"),
        (passport(attributes={"brand": "honda", "model": "lead"}), "Мотобайк Honda Lead, Нячанг"),
        (passport(city=None), "Мотобайк"),
        # Сторона сделки названа, когда нарушает умолчание категории.
        (passport(intent=Intent.RENT), "Аренда: мотобайк, Нячанг"),
        (passport(intent=Intent.SELL), "Продажа: мотобайк, Нячанг"),
        (passport(category=Category.APARTMENT, intent=Intent.BUY), "Покупка: квартира, Нячанг"),
        (passport(category=Category.APARTMENT, intent=Intent.RENT_OUT), "Сдача: квартира, Нячанг"),
    ],
    ids=[
        "scooter",
        "motorbike",
        "no_intent",
        "apartment_rent_is_the_default",
        "brand_and_model",
        "no_city",
        "motorbike_rent",
        "motorbike_sell",
        "apartment_buy",
        "apartment_rent_out",
    ],
)
def test_the_title_names_the_subject_the_side_of_the_deal_and_the_city(
    given: Passport, expected: str
) -> None:
    assert threads.title(given) == expected


@pytest.mark.parametrize(
    ("buy", "rent"),
    [
        ("куплю байк в нячанге до 500 долларов", "сниму байк в нячанге посуточно"),
        ("куплю квартиру в нячанге", "сниму квартиру в нячанге до 10 млн"),
    ],
    ids=["bike", "flat"],
)
def test_buying_and_renting_the_same_thing_do_not_share_a_title(buy: str, rent: str) -> None:
    """Живой пример из разбора: две одинаковые кнопки, а человек переключается не туда."""
    assert threads.title(parse_query(buy)) != threads.title(parse_query(rent))


@pytest.mark.parametrize(
    ("brand", "model", "expected"),
    [
        ("honda", "sh150i", "Мотобайк Honda sh150i, Нячанг"),
        ("honda", "PCX", "Мотобайк Honda PCX, Нячанг"),
        ("yamaha", "MT-15", "Мотобайк Yamaha MT-15, Нячанг"),
        ("kawasaki", "z300", "Мотобайк Kawasaki z300, Нячанг"),
        ("honda", "lead", "Мотобайк Honda Lead, Нячанг"),
    ],
)
def test_model_names_keep_their_own_letters(brand: str, model: str, expected: str) -> None:
    """`str.title()` калечил модели: «Sh150I», «Pcx», «Mt-15». Заглавная — только у слов из букв."""
    given = passport(attributes={"brand": brand, "model": model})

    assert threads.title(given) == expected


def test_a_title_without_a_category_falls_back_to_the_words_said() -> None:
    """Категории нет — звать поиск нечем, кроме сказанного: пустая кнопка хуже."""
    assert threads.title(passport(category=None, raw_query="honda до 300")) == "Honda до 300"


def test_the_fallback_drops_the_service_tail_and_extra_lines() -> None:
    """Правка дописывает к формулировке служебный хвост после перевода строки.

    Он описывает механику («заменяет прежние условия»), а не поиск, и в кнопку
    попадать не должен — как и вторая строка многострочного сообщения.
    """
    tail = "что-нибудь\nПоследнее уточнение (заменяет прежние условия): до 500"

    assert threads.title(passport(category=None, raw_query=tail)) == "Что-нибудь"
    assert threads.title(passport(category=None, raw_query="\n\nищу\nчто-то")) == "Ищу"


def test_the_title_does_not_follow_the_last_wording() -> None:
    """Подпись кнопки не прыгает от правок: иначе поиск не найти глазами.

    Формулировка меняется на каждом уточнении («до 500», «не скутер, а
    мотоцикл»), а человек ищет в списке ту строку, которую запомнил.
    """
    first = passport(raw_query="ищу скутер в Нячанге до 400 долларов")
    edited = passport(
        raw_query="до 500\nПоследнее уточнение (заменяет прежние условия): до 500",
        budget=Budget(max=500, currency=Currency.USD),
    )

    assert threads.title(first) == threads.title(edited)


# ── длина и посторонние символы ─────────────────────────────────────────────


def test_a_long_title_is_cut_to_fit_a_telegram_button() -> None:
    long = passport(attributes={"brand": "honda", "model": "super cub c125 final edition"})

    label = threads.title(long)

    assert len(label) <= threads.TITLE_LIMIT
    assert label.endswith("…")


def test_a_huge_wording_is_cut_too() -> None:
    label = threads.title(passport(category=None, raw_query="а" * 5000))

    assert len(label) <= threads.TITLE_LIMIT


def test_control_and_invisible_characters_never_reach_the_title() -> None:
    """Перевод строки ломает ряд кнопок, нулевой пробел и RLO подделывают подпись."""
    nasty = passport(category=None, raw_query="что\tни\x00бу​дь\x07‮  дёшево")

    label = threads.title(nasty)

    assert label == "Что нибудь дёшево"
    assert not re.search(r"[\x00-\x08\x0b-\x1f​‮]", label)


def test_brand_and_model_are_cleaned_too() -> None:
    given = passport(attributes={"brand": "ho\nnda", "model": "le​ad"})

    label = threads.title(given)

    assert "\n" not in label
    assert "​" not in label


# ── кнопка — текст, сообщение — HTML ────────────────────────────────────────


def test_the_title_is_plain_text_for_a_button_and_escaped_for_a_message() -> None:
    """Подпись кнопки — обычный текст: экранирование показало бы человеку `&lt;`."""
    angry = passport(category=None, raw_query="что-нибудь <300$ & <b>")

    assert threads.title(angry) == "Что-нибудь <300$ & <b>"
    bold = threads.bold_title(angry)
    assert bold == "<b>Что-нибудь &lt;300$ &amp; &lt;b&gt;</b>"


def test_every_message_that_carries_a_title_escapes_it() -> None:
    """Карточка и «Изменяем» — те самые старые места, которые теперь питает название."""
    angry = passport(category=None, raw_query="<script>&")
    item = QueryOverview(root=1, passport=angry, monitoring="active")

    for text in (threads.card_text(item), threads.edit_prompt(angry)):
        assert "<script>" not in text
        assert "&lt;script&gt;&amp;" in text


# ── подписи списка: различимость ────────────────────────────────────────────


def test_twin_titles_are_told_apart_by_budget() -> None:
    cheap = passport(budget=Budget(max=300, currency=Currency.USD))
    dear = passport(budget=Budget(max=1000, currency=Currency.USD))

    cheap_label, dear_label = threads.labels([cheap, dear])

    assert cheap_label != dear_label
    assert "до 300 USD" in cheap_label
    assert "до 1 000 USD" in dear_label
    assert cheap_label.startswith("Мотобайк, Нячанг")


def test_unique_titles_stay_plain_so_the_label_does_not_jump_on_a_budget_edit() -> None:
    """Бюджет — различитель на случай совпадения, а не часть названия.

    Иначе каждая правка «до 500» меняла бы подпись, и человек искал бы в списке
    строку, которой уже нет.
    """
    flat = passport(category=Category.APARTMENT, intent=Intent.RENT, city="nha_trang")
    before = threads.labels([passport(), flat])
    after = threads.labels([passport(budget=Budget(max=500, currency=Currency.USD)), flat])

    assert before == after == ["Мотобайк, Нячанг", "Квартира, Нячанг"]


def test_twins_without_a_budget_have_nothing_to_tell_them_apart_by() -> None:
    """Совсем одинаковые поиски остаются одинаковыми: различать их нечем, и это честно."""
    plain = passport(budget=Budget())

    assert threads.labels([plain, plain]) == ["Мотобайк, Нячанг", "Мотобайк, Нячанг"]


@pytest.mark.parametrize(
    "budget",
    [
        Budget(max=400, currency=Currency.USD),
        Budget(max=10_000_000, currency=Currency.VND),
        Budget(max=1_500_000_000, currency=Currency.VND),
    ],
    ids=["usd", "vnd", "billions"],
)
def test_a_label_with_a_budget_still_fits_the_button(budget: Budget) -> None:
    long = passport(attributes={"brand": "honda", "model": "super cub c125"}, budget=budget)
    other = passport(
        attributes={"brand": "honda", "model": "super cub c125"},
        budget=Budget(max=5, currency=Currency.USD),
    )

    for label in threads.labels([long, other]):
        assert len(label) <= threads.TITLE_LIMIT, label
    assert threads.labels([long, other])[0] != threads.labels([long, other])[1]
