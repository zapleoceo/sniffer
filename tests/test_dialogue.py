"""Правила уточняющего диалога: что спросить, что сделать с ответом.

Домен проверяется отдельно от бота: выбор вопроса — продуктовое правило
(passport.md), и оно обязано быть проверяемым без Telegram, базы и модели.
"""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.domain.dialogue import (
    EVENT_MANUAL_EDIT,
    EVENT_QUESTION_ASKED,
    SKIP,
    DialogueState,
    Feedback,
    _contradicts,
    _contradicts_budget,
    _same_stem,
    apply_answer,
    apply_feedback,
    feedback_buttons,
    feedback_question,
    next_question,
    parse_option,
    replay,
    restates,
)
from sniffer.domain.passport import (
    FIELD_INFORMATIVENESS,
    MAX_CLARIFYING_QUESTIONS,
    MAX_FEEDBACK_QUESTIONS,
    Budget,
    Category,
    Currency,
    Intent,
    Passport,
    PassportStatus,
)
from sniffer.domain.records import PassportEvent


def bike(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


def event(kind: str, **payload: object) -> PassportEvent:
    return PassportEvent(passport_id=1, kind=kind, payload=payload)


# ── выбор вопроса ───────────────────────────────────────────────────────────


def test_full_passport_has_nothing_to_ask() -> None:
    """Заполненный паспорт вопросов не порождает: спрашивать нечего, надо искать."""
    filled = bike(
        budget=Budget(max=400, currency=Currency.USD),
        attributes={"transmission": "automatic", "condition": "good", "brand": "honda"},
    )

    assert next_question(filled, asked=()) is None


def test_the_most_informative_field_goes_first() -> None:
    """Бюджет режет выборку вдвое, марка — почти нет (FIELD_INFORMATIVENESS)."""
    question = next_question(bike(), asked=())

    assert question is not None
    assert question.field == "budget.max"


def test_asked_field_is_not_asked_twice() -> None:
    question = next_question(bike(), asked=("budget.max",))

    assert question is not None
    assert question.field == "attributes.transmission"


def test_three_questions_is_the_ceiling() -> None:
    """Дальше показываем выдачу: допрос убивает конверсию сильнее неточной выдачи."""
    asked = ("budget.max", "attributes.transmission", "attributes.condition")

    assert len(asked) == MAX_CLARIFYING_QUESTIONS
    assert next_question(bike(), asked=asked) is None


def test_unknown_category_is_asked_first() -> None:
    question = next_question(Passport(raw_query="что-нибудь"), asked=())

    assert question is not None
    assert question.field == "category"


def test_every_question_can_be_skipped() -> None:
    """Клиент, которого допрашивают без права пропустить, уходит."""
    question = next_question(bike(), asked=())

    assert question is not None
    assert question.buttons[-1].value == SKIP


# ── ответ ───────────────────────────────────────────────────────────────────


def test_button_answer_fills_the_budget() -> None:
    answered = apply_answer(bike(), "budget.max", parse_option("budget.max", "500 USD"))

    assert answered.budget.max == 500
    assert answered.budget.currency is Currency.USD


def test_words_answer_keeps_its_own_currency() -> None:
    """«До 10 млн» — донги, и пересчитывать их в доллары мы не вправе."""
    answered = apply_answer(bike(), "budget.max", Budget(max=10_000_000, currency=Currency.VND))

    assert (answered.budget.max, answered.budget.currency) == (10_000_000, Currency.VND)


def test_answer_does_not_touch_the_previous_passport() -> None:
    """Паспорт неизменяем: правка создаёт новый объект, а не меняет старый."""
    before = bike()

    apply_answer(before, "attributes.transmission", "automatic")

    assert before.attributes == {}


def test_answer_updates_what_is_still_missing() -> None:
    answered = apply_answer(bike(), "budget.max", 400)

    assert "budget.max" not in answered.missing_fields
    assert answered.status is PassportStatus.READY


def test_category_answer_switches_the_question_set() -> None:
    answered = apply_answer(Passport(raw_query="что-нибудь"), "category", "apartment")
    question = next_question(answered, asked=("budget.max",))

    assert answered.category is Category.APARTMENT
    assert question is not None
    assert question.field == "attributes.rooms"


def test_unknown_field_is_a_bug_not_a_silent_no_op() -> None:
    with pytest.raises(ValueError, match="не заполняется"):
        apply_answer(bike(), "confidence", "0.9")


# ── обратная связь ──────────────────────────────────────────────────────────


def test_pricey_cuts_the_budget() -> None:
    cheaper = apply_feedback(bike(budget=Budget(max=500, currency=Currency.USD)), Feedback.PRICEY)

    assert cheaper is not None
    assert cheaper.budget.max == 350


def test_pricey_without_a_budget_asks_instead_of_guessing() -> None:
    """Сколько «дорого» в цифрах, мы не знаем: бюджет не назывался."""
    passport = bike()

    assert apply_feedback(passport, Feedback.PRICEY) is None
    question = feedback_question(passport, Feedback.PRICEY, asked=())
    assert question is not None
    assert question.field == "budget.max"


def test_automatic_feedback_fills_transmission() -> None:
    fixed = apply_feedback(bike(), Feedback.AUTOMATIC)

    assert fixed is not None
    assert fixed.attributes["transmission"] == "automatic"


def test_wrong_feedback_asks_one_more_question_over_the_limit() -> None:
    """Три вопроса — защита от допроса ДО выдачи; здесь клиент сам нажал кнопку."""
    asked = ("budget.max", "attributes.transmission", "attributes.condition")

    assert next_question(bike(), asked=asked) is None
    question = feedback_question(bike(), Feedback.WRONG, asked=asked)
    assert question is not None
    assert question.field == "attributes.brand"


def test_feedback_ceiling_does_not_grow_with_each_press(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потолок обратной связи абсолютный: MAX+1, а не «на один больше заданных».

    Пятое информативное поле — то, чего у motorbike сегодня нет: без него
    правило держится случайно, на длине каталога, и молча ломается в тот день,
    когда поле добавят.
    """
    monkeypatch.setitem(FIELD_INFORMATIVENESS[Category.MOTORBIKE], "attributes.rooms", 0.05)
    asked = (
        "budget.max",
        "attributes.transmission",
        "attributes.condition",
        "attributes.brand",
    )

    assert len(asked) == MAX_FEEDBACK_QUESTIONS
    assert next_question(bike(), asked=asked[:1]) is not None, "спросить ещё есть что"
    assert feedback_question(bike(), Feedback.WRONG, asked=asked) is None


def test_automatic_button_is_offered_only_where_it_means_something() -> None:
    values = {option.value for option in feedback_buttons(bike())}
    apartment = {option.value for option in feedback_buttons(bike(category=Category.APARTMENT))}

    assert Feedback.AUTOMATIC.value in values
    assert Feedback.AUTOMATIC.value not in apartment
    assert {Feedback.PRICEY.value, Feedback.WRONG.value} <= apartment


# ── та же просьба или новая ─────────────────────────────────────────────────


def test_the_same_wording_is_the_same_request() -> None:
    """Повтор фразы не должен обнулять собранное: это не новый запрос."""
    assert restates(bike(raw_query="ищу скутер в нячанге"), bike(raw_query="ищу скутер в нячанге"))
    assert restates(
        bike(raw_query="ищу скутер в нячанге"), bike(raw_query="Ищу скутер в Нячанге!")
    ), "регистр и знаки формулировку не меняют"
    assert restates(bike(raw_query="ищу скутер в нячанге"), bike(raw_query="скутер в нячанге")), (
        "короче — та же просьба"
    )


def test_another_city_is_a_new_request_even_with_the_same_words() -> None:
    """Слов общих много, а запрос другой: решает разбор, а не похожесть текста."""
    assert not restates(
        bike(raw_query="ищу скутер в нячанге"),
        bike(city="da_nang", raw_query="ищу скутер в дананге"),
    )


def test_another_category_is_a_new_request() -> None:
    assert not restates(
        bike(raw_query="ищу скутер"),
        bike(category=Category.APARTMENT, raw_query="ладно, тогда квартиру в Нячанге"),
    )


def test_a_telegraphic_repeat_is_the_same_request() -> None:
    """Падеж слова просьбу не меняет: «скутер нячанг» — та же просьба.

    Падало до правки: слова сравнивались буква в букву, «нячанг» и «нячанге»
    считались разными, и телеграфный повтор обнулял собранные ответы.
    """
    assert restates(bike(raw_query="ищу скутер в нячанге"), bike(raw_query="скутер нячанг"))
    assert restates(bike(raw_query="ищу скутер в нячанге"), bike(raw_query="нячанг скутер")), (
        "порядок слов клиент меняет свободно"
    )


@pytest.mark.parametrize("other", ["хойане", "вунгтау", "далате", "ханое", "куангнгае"])
def test_a_replaced_word_is_not_a_repeat_whatever_the_dictionary_knows(other: str) -> None:
    """Смену города решает новое слово, а не доля общих и не справочник городов.

    Город в паспорте здесь одинаковый нарочно: так проверяется именно половина
    со словами. У «ищу скутер в хойане» против «ищу скутер в нячанге» ровно 0.6
    общих слов — то есть прежний порог решался знаком сравнения, и «куангнгае»,
    которого нет ни в одном справочнике, проходил бы за повтор так же.
    """
    assert not restates(
        bike(raw_query="ищу скутер в нячанге"), bike(raw_query=f"ищу скутер в {other}")
    )


def test_other_words_are_a_new_request_even_when_the_fields_match() -> None:
    """Разбор мог не увидеть смену темы — тогда клиента слушают по словам.

    Разбор читает три поля, и совпасть по всем трём смена темы вполне может:
    «ладно, тогда квартиру» настоящие правила разберут в `apartment` и поймают
    сами, а вот телеграфную или перефразированную смену — не обязательно. Здесь
    категория совпадает нарочно: проверяется именно половина со словами.
    """
    assert not restates(
        bike(raw_query="ищу скутер"), bike(raw_query="ладно, тогда квартиру в Нячанге")
    )


def test_an_empty_wording_is_not_a_repeat() -> None:
    """Сравнивать нечего — значит, и повтором это считать нельзя."""
    assert not restates(bike(raw_query=""), bike(raw_query="ищу скутер"))


# ── состояние диалога ───────────────────────────────────────────────────────


def test_state_is_replayed_from_the_event_log() -> None:
    state = replay(
        [
            event("user_message", text="ищу скутер"),
            event(EVENT_QUESTION_ASKED, field="budget.max"),
            event(EVENT_MANUAL_EDIT, field="budget.max", value="500"),
            event(EVENT_QUESTION_ASKED, field="attributes.transmission"),
        ]
    )

    assert state == DialogueState(
        asked=("budget.max", "attributes.transmission"), pending="attributes.transmission"
    )


def test_answer_closes_the_pending_question() -> None:
    state = replay(
        [
            event(EVENT_QUESTION_ASKED, field="budget.max"),
            event(EVENT_MANUAL_EDIT, field="budget.max", skipped=True),
        ]
    )

    assert state.pending is None
    assert state.asked == ("budget.max",), "пропуск не отменяет того, что вопрос был задан"


# --------------------------------------------------------------------------
# Вторая проверка: где «новое слово» врало в обе стороны. Каждый случай ниже
# был замерен живьём, поэтому лежит отдельным тестом, а не одним общим.
# --------------------------------------------------------------------------


def _asked(text: str, **fields: Any) -> Passport:
    """Паспорт с этой формулировкой и явно названными полями."""
    base: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": text,
    }
    base.update(fields)
    return Passport(**base)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(10_000, 1_000, id="доллары-в-десять-раз-дешевле"),
        pytest.param(12_000_000, 1_200_000, id="донги-полными-цифрами"),
        pytest.param(8_000_000, 8_000, id="три-нуля-потеряны"),
        pytest.param(1_000, 100_000, id="дороже-в-сто-раз"),
    ],
)
def test_a_changed_budget_is_a_new_request_not_a_repeat(before: float, after: float) -> None:
    """Клиент передумал по сумме — это новый запрос, а не та же просьба.

    Замер показал потерю данных: `restates` сравнивал три поля и бюджета среди
    них не было, а половина со словами склеивала «1000» и «10000» по общей
    основе. Правка в десять раз молча выбрасывалась, и искали по прежней сумме.
    """
    current = _asked(
        f"квартира в нячанге до {before:.0f} долларов",
        category=Category.APARTMENT,
        budget=Budget(max=before, currency=Currency.USD),
    )
    fresh = _asked(
        f"квартира в нячанге до {after:.0f} долларов",
        category=Category.APARTMENT,
        budget=Budget(max=after, currency=Currency.USD),
    )

    assert not restates(current, fresh)


def test_the_same_number_in_another_currency_is_a_new_request() -> None:
    """300 донгов и 300 долларов — не одна сумма, а разница в двадцать тысяч раз."""
    current = _asked("скутер до 300", budget=Budget(max=300, currency=Currency.USD))
    fresh = _asked("скутер до 300 донгов", budget=Budget(max=300, currency=Currency.VND))

    assert not restates(current, fresh)


def test_a_repeat_that_names_no_budget_keeps_the_collected_one() -> None:
    """Молчание про бюджет — не «бюджет другой», а «про бюджет ничего не сказал».

    Иначе любой короткий повтор после собранного ответа стирал бы этот ответ.
    """
    current = _asked("ищу скутер в нячанге", budget=Budget(max=400, currency=Currency.USD))
    fresh = _asked("скутер нячанг")

    assert restates(current, fresh)


def test_a_changed_attribute_is_a_new_request() -> None:
    """«Нужен автомат» → «нужна механика»: сменился запрос, а не формулировка."""
    current = _asked("ищу скутер автомат в нячанге", attributes={"transmission": "automatic"})
    fresh = _asked("ищу скутер механика в нячанге", attributes={"transmission": "manual"})

    assert not restates(current, fresh)


@pytest.mark.parametrize(
    "addition",
    [
        pytest.param("а", id="одна-буква"),
        pytest.param("а б", id="две-буквы"),
        pytest.param("срочно", id="вежливость"),
        pytest.param("пожалуйста", id="ещё-вежливость"),
    ],
)
def test_appending_a_filler_word_does_not_reset_the_question_limit(addition: str) -> None:
    """Дописанное служебное слово — тот же запрос, а не новый.

    Именно так обходился лимит трёх вопросов: «ищу скутер в нячанге», потом
    «…а», потом «…а б» — каждый раз новая цепочка и новые три вопроса, живьём
    получалось девять. Слово, не несущее содержания, повтором быть не мешает.
    """
    current = _asked("ищу скутер в нячанге")
    fresh = _asked(f"ищу скутер в нячанге {addition}")

    assert restates(current, fresh)


def test_a_preposition_added_to_the_same_words_is_still_a_repeat() -> None:
    """«квартира в нячанге мебель» → «…с мебелью»: предлог сведений не приносит."""
    current = _asked("квартира в нячанге мебель", category=Category.APARTMENT)
    fresh = _asked("квартира в нячанге с мебелью", category=Category.APARTMENT)

    assert restates(current, fresh)


def test_a_telegraphic_repeat_works_in_both_directions() -> None:
    """Исход не должен зависеть от того, какую фразу клиент напечатал первой.

    «скутер нячанг» → «ищу скутер в нячанге» добавляет «ищу» и «в» — оба
    служебные, значит повтор. Обратный порядок повтором был и раньше.
    """
    short, full = _asked("скутер нячанг"), _asked("ищу скутер в нячанге")

    assert restates(short, full)
    assert restates(full, short)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("дом", "дома", id="дом-дома"),
        pytest.param("дом", "домик", id="дом-домик"),
        pytest.param("нячанг", "нячанге", id="нячанг-нячанге"),
        pytest.param("квартира", "квартиру", id="квартира-квартиру"),
        pytest.param("сдам", "сдаю", id="сдам-сдаю"),
    ],
)
def test_one_word_in_two_forms_is_one_word(first: str, second: str) -> None:
    """Падеж и уменьшительное — то же слово. «дом»/«дома» четырёх букв не
    набирали и считались разными, хотя это чистая словоформа."""
    assert _same_stem(first, second)
    assert _same_stem(second, first)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("куангнгае", "куангнаме", id="два-города-вьетнама"),
        pytest.param("куангнинь", "куангбинь", id="ещё-два-города"),
        pytest.param("автомат", "автобус", id="автомат-автобус"),
        pytest.param("квартал", "квартира", id="квартал-квартира"),
        pytest.param("студент", "студия", id="студент-студия"),
        pytest.param("1000", "10000", id="числа-в-десять-раз"),
        pytest.param("500", "5000", id="ещё-числа"),
    ],
)
def test_different_words_stay_different(first: str, second: str) -> None:
    """Общее начало — ещё не общий корень.

    Фиксированные четыре буквы склеивали «Куангнгай» с «Куангнамом» (два разных
    города, которых нет ни в одном справочнике) и «1000» с «10000». У числа
    морфологии нет вовсе, поэтому числа сравниваются точно.
    """
    assert not _same_stem(first, second)
    assert not _same_stem(second, first)


def test_two_unlisted_cities_are_two_requests() -> None:
    """Города не из справочника: город обоих не разобран, различить их может
    только слово — и оно обязано их различить."""
    current = _asked("ищу скутер в куангнгае", city=None)
    fresh = _asked("ищу скутер в куангнаме", city=None)

    assert not restates(current, fresh)


def test_a_button_label_and_its_value_name_the_same_currency() -> None:
    """Подпись говорит «$» — значит уедет USD, чем бы ни был паспорт.

    Замер: клиент сказал «сниму квартиру в нячанге за донги», паспорт получил
    VND, и кнопка «до 300 $» отправляла 300 донгов — 1.2 цента. Валюта теперь
    лежит в значении кнопки, рядом с подписью, а не берётся из паспорта.
    """
    in_dong = _asked(
        "сниму квартиру в нячанге за донги",
        intent=Intent.RENT_OUT,
        category=Category.APARTMENT,
        budget=Budget(currency=Currency.VND),
    )

    answered = apply_answer(in_dong, "budget.max", parse_option("budget.max", "300 USD"))

    assert (answered.budget.max, answered.budget.currency) == (300, Currency.USD)


def test_a_new_ceiling_does_not_leave_the_range_inside_out() -> None:
    """«От 3 млн донгов» плюс кнопка «до 300 $» давали min=3000000, max=300.

    Из такого диапазона не собирается никакой фильтр. Проигрывает старая
    нижняя граница: клиент только что назвал верхнюю, о ней он и говорил.
    """
    with_floor = _asked(
        "сниму квартиру в нячанге от 3 млн донгов",
        intent=Intent.RENT_OUT,
        category=Category.APARTMENT,
        budget=Budget(min=3_000_000, currency=Currency.VND),
    )

    answered = apply_answer(with_floor, "budget.max", parse_option("budget.max", "300 USD"))

    assert answered.budget.min is None
    assert (answered.budget.max, answered.budget.currency) == (300, Currency.USD)


def test_a_floor_turned_into_a_ceiling_is_a_new_request() -> None:
    """«От 500 долларов» → «до 500 долларов»: запрос противоположный.

    Половина со словами здесь бессильна и это правильно: «от» и «до» —
    предлоги, и считать их новым словом значило бы вернуть обход лимита
    вопросов. Различает такие пары только сравнение самих фактов, поэтому тест
    и стоит отдельно: без него слой сравнения фактов ничего не проверял —
    остальные случаи закрывались словами.
    """
    floor = _asked(
        "квартира в нячанге от 500 долларов",
        category=Category.APARTMENT,
        budget=Budget(min=500, currency=Currency.USD),
    )
    ceiling = _asked(
        "квартира в нячанге до 500 долларов",
        category=Category.APARTMENT,
        budget=Budget(max=500, currency=Currency.USD),
    )

    assert not restates(floor, ceiling)
    assert not restates(ceiling, floor)


def test_a_negation_is_not_a_filler_word() -> None:
    """«Скутер автомат» → «скутер не автомат»: просьба перевернулась.

    Отрицание короткое и служебное на вид, поэтому в список служебных слов оно
    просилось само. Попади оно туда — смена запроса читалась бы как повтор.
    """
    wants = _asked("ищу скутер автомат в нячанге")
    refuses = _asked("ищу скутер не автомат в нячанге")

    assert not restates(wants, refuses)


def test_silence_about_a_field_is_not_a_contradiction() -> None:
    """Новая формулировка не назвала валюту — прежняя остаётся.

    Иначе «до 1000» после «до 1000 долларов» читалось бы как смена валюты и
    обнуляло бы собранные ответы.
    """
    said_in_dollars = Budget(max=1000, currency=Currency.USD)

    assert not _contradicts_budget(said_in_dollars, Budget(max=1000))
    assert not _contradicts_budget(said_in_dollars, Budget())
    assert _contradicts_budget(said_in_dollars, Budget(max=1000, currency=Currency.VND))
    assert _contradicts_budget(said_in_dollars, Budget(max=200, currency=Currency.USD))


def test_a_changed_attribute_contradicts_even_when_the_words_repeat() -> None:
    """Атрибут сравнивается так же: назвал иначе — новый запрос, промолчал — нет."""
    collected = _asked("ищу скутер автомат", attributes={"transmission": "automatic"})

    assert _contradicts(collected, _asked("ищу скутер", attributes={"transmission": "manual"}))
    assert not _contradicts(collected, _asked("ищу скутер"))
    assert not _contradicts(collected, _asked("ищу скутер", attributes={"papers": "blue_card"}))


def test_a_bare_number_still_means_dollars() -> None:
    """«500» без валюты — доллары, а не «валюта неизвестна».

    Кнопка теперь называет валюту сама, и легко было отнять доллар по умолчанию
    у голого числа, которое клиент пишет словами чаще всего.
    """
    answered = apply_answer(bike(), "budget.max", parse_option("budget.max", "500"))

    assert (answered.budget.max, answered.budget.currency) == (500, Currency.USD)
