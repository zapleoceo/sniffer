"""Реестр полей, про которые бот спрашивает: один объект на поле.

Раньше знание о поле было разложено по четырём переключателям: список
`QUESTIONS`, `parse_option` (кнопка → значение), `apply_answer` (значение →
паспорт) и `interpret` в `search/answers.py` (слова → значение). Новое поле —
правка во всех четырёх, и забыть одно значило получить кнопку, которую нечем
разобрать. Теперь поле — одна запись `FieldSpec`; четвёртое звено (слова)
живёт в `search/`, потому что оно читает словарь рынка, а домену его знать
нельзя, и подключается там по ТОМУ ЖЕ ключу `field`.

Поведение переехало без изменений: `tests/test_fields_registry.py` сверяет
реестр с дословной копией прежних функций на комбинациях паспортов и значений.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sniffer.domain.passport import Budget, Category, Currency, Passport, PassportStatus, has_value
from sniffer.domain.questions import AnswerValue, Option, Question

# Готовые суммы бюджета. Валюта лежит в той же строке, что и подпись, и
# значение кнопки строится из обеих — иначе они расходятся молча: подпись
# говорила «до 300 $», а уезжала валюта ПАСПОРТА, и клиенту, сказавшему «за
# донги», кнопка «до 300 $» отправляла 300 донгов — это 1.2 цента.
_BUDGET_CHOICES: tuple[tuple[int, Currency], ...] = (
    (300, Currency.USD),
    (500, Currency.USD),
    (800, Currency.USD),
)
_CURRENCY_SIGN: dict[Currency, str] = {Currency.USD: "$", Currency.VND: "₫"}

Parse = Callable[[str], AnswerValue]
Apply = Callable[[Passport, AnswerValue], Passport]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Всё, что бот знает про одно поле: вопрос, кнопка, применение ответа.

    `facet` — ключ отчёта по базе (`domain/facets.py`), по которому планировщик
    строит динамический вопрос с числами; `None` — поле есть, но база его не
    считает, и спрашивается оно только статическим текстом.
    """

    question: Question
    parse: Parse
    apply: Apply
    facet: str | None = None

    @property
    def field(self) -> str:
        return self.question.field

    @property
    def code(self) -> str:
        return self.question.code


def _raw(raw: str) -> AnswerValue:
    return raw


def _parse_budget(raw: str) -> AnswerValue:
    """Бюджет приезжает вместе с валютой («300 USD»), потому что подпись кнопки
    называет валюту, а значение обязано называть ту же: догадываться о ней по
    паспорту нельзя — там могут быть донги, и тогда «до 300 $» превратилось бы
    в 300 донгов."""
    amount, _, currency = raw.partition(" ")
    return Budget(max=float(amount), currency=Currency(currency) if currency else None)


def _parse_rooms(raw: str) -> AnswerValue:
    return int(raw)


def finished(passport: Passport, update: dict[str, object]) -> Passport:
    """Применить правку и пересчитать, чего ещё не хватает и готов ли паспорт."""
    revised = passport.model_copy(update=update)
    return revised.model_copy(
        update={
            "missing_fields": [
                name for name in ("category", "city", "budget.max") if not has_value(revised, name)
            ],
            "status": PassportStatus.READY if revised.is_ready() else revised.status,
        }
    )


def _apply_budget(passport: Passport, value: AnswerValue) -> Passport:
    return finished(passport, {"budget": _budget(passport, value)})


def _apply_category(passport: Passport, value: AnswerValue) -> Passport:
    update: dict[str, object] = {
        "category": Category.MOTORBIKE if value == "scooter" else Category(str(value))
    }
    if value == "scooter":
        update["attributes"] = {
            **passport.attributes,
            "transmission": "automatic",
            "body_type": "tay_ga",
        }
    return finished(passport, update)


def _apply_city(passport: Passport, value: AnswerValue) -> Passport:
    return finished(passport, {"city": str(value)})


def apply_attribute(passport: Passport, key: str, value: AnswerValue) -> Passport:
    return finished(passport, {"attributes": {**passport.attributes, key: value}})


def _attribute(key: str) -> Apply:
    return lambda passport, value: apply_attribute(passport, key, value)


def _budget(passport: Passport, value: AnswerValue) -> Budget:
    """Ответ про сумму → бюджет. Валюта берётся у ответа, если он её назвал.

    Период не трогаем: его выбрал разбор запроса по намерению (аренда —
    помесячно, покупка — разово), и ответ про сумму об этом ничего не говорит.

    Нижняя граница сохраняется, но только пока она не спорит с новой верхней:
    «от 3 млн донгов» плюс кнопка «до 300 $» давали `min=3000000, max=300` —
    диапазон наизнанку, из которого никакой фильтр не соберётся. Проиграет
    старая граница: клиент только что назвал верхнюю, о ней он и говорил.
    """
    if isinstance(value, Budget):
        # Валюта ответа главнее паспортной: подпись кнопки её называет. Доллар
        # остаётся последним доводом — голое «500» без валюты приходит и от
        # клиента словами, и оно означает доллары, а не «валюта неизвестна».
        currency = value.currency or passport.budget.currency or Currency.USD
        top = value.max
    else:
        currency = passport.budget.currency or Currency.USD
        top = float(value)
    return Budget(
        min=_keep_floor(passport.budget, top, currency),
        max=top,
        currency=currency,
        period=passport.budget.period,
    )


def _keep_floor(current: Budget, top: float | None, currency: Currency | None) -> float | None:
    """Прежняя нижняя граница — если она всё ещё ниже верхней и в той же валюте."""
    floor = current.min
    if floor is None or top is None:
        return floor
    if current.currency is not None and current.currency != currency:
        # Границы в разных валютах — это не диапазон, а два разных числа.
        return None
    return floor if floor < top else None


# Спрашиваем только про то, что умеем спросить кнопками. Поле без записи
# (например `districts` — справочника районов пока нет) просто пропускается:
# лучше не спросить, чем спросить так, что ответ нечем разобрать.
SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        Question(
            field="category",
            code="cat",
            text="Что ищем?",
            options=(
                Option("скутер", "scooter"),
                Option("мотобайк", "motorbike"),
                Option("квартиру", "apartment"),
                Option("комнату", "room"),
                Option("дом", "house"),
            ),
            skippable=False,
        ),
        _raw,
        _apply_category,
    ),
    FieldSpec(
        Question(
            field="city",
            code="city",
            text="В каком городе ищем?",
            options=(Option("Нячанг", "nha_trang"), Option("Дананг", "da_nang")),
            skippable=False,
        ),
        _raw,
        _apply_city,
    ),
    FieldSpec(
        Question(
            field="budget.max",
            code="budget",
            text="Какой бюджет? Можно написать словами — «до 400» или «до 10 млн».",
            options=tuple(
                Option(f"до {amount} {_CURRENCY_SIGN[currency]}", f"{amount} {currency.value}")
                for amount, currency in _BUDGET_CHOICES
            ),
        ),
        _parse_budget,
        _apply_budget,
        facet="budget.max",
    ),
    FieldSpec(
        Question(
            field="attributes.transmission",
            code="trans",
            text="Автомат или механика?",
            options=(Option("автомат", "automatic"), Option("механика", "manual")),
        ),
        _raw,
        _attribute("transmission"),
        facet="attributes.transmission",
    ),
    FieldSpec(
        Question(
            field="attributes.condition",
            code="cond",
            text="Состояние?",
            options=(
                Option("новый", "new"),
                Option("хороший", "good"),
                Option("любой, лишь бы ездил", "worn"),
            ),
        ),
        _raw,
        _attribute("condition"),
    ),
    FieldSpec(
        Question(
            field="attributes.brand",
            code="brand",
            text="Есть марка на примете?",
            options=(Option("Honda", "honda"), Option("Yamaha", "yamaha")),
        ),
        _raw,
        _attribute("brand"),
        facet="attributes.brand",
    ),
    FieldSpec(
        Question(
            field="attributes.model",
            code="model",
            text="Какая модель?",
        ),
        _raw,
        _attribute("model"),
        facet="attributes.model",
    ),
    FieldSpec(
        Question(
            field="attributes.rooms",
            code="rooms",
            text="Сколько комнат?",
            options=(Option("студия", "1"), Option("две", "2"), Option("три и больше", "3")),
        ),
        _parse_rooms,
        _attribute("rooms"),
        facet="attributes.rooms",
    ),
)

BY_FIELD: dict[str, FieldSpec] = {spec.field: spec for spec in SPECS}
BY_CODE: dict[str, FieldSpec] = {spec.code: spec for spec in SPECS}
# Прежний кортеж вопросов — производный: единственный источник правды теперь реестр.
QUESTIONS: tuple[Question, ...] = tuple(spec.question for spec in SPECS)


def question_for(field: str) -> Question | None:
    spec = BY_FIELD.get(field)
    return spec.question if spec is not None else None


def question_by_code(code: str) -> Question | None:
    spec = BY_CODE.get(code)
    return spec.question if spec is not None else None


def parse_option(field: str, raw: str) -> AnswerValue:
    """Значение кнопки → значение поля паспорта."""
    spec = BY_FIELD.get(field)
    return spec.parse(raw) if spec is not None else raw


def apply_answer(passport: Passport, field: str, value: AnswerValue) -> Passport:
    """Ответ клиента → новая версия паспорта.

    Паспорт неизменяем (passport.md): здесь появляется новый объект, а версию
    ему присваивает репозиторий. Пропуск («не важно») сюда не доходит вовсе —
    он ничего не меняет и остаётся только событием.
    """
    spec = BY_FIELD.get(field)
    if spec is not None:
        return spec.apply(passport, value)
    if field.startswith("attributes."):
        return apply_attribute(passport, field.removeprefix("attributes."), value)
    raise ValueError(f"поле {field!r} не заполняется ответом клиента")
