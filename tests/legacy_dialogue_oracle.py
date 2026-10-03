"""Прежние `QUESTIONS` / `parse_option` / `apply_answer`, снятые дословно до перехода на реестр.

Это оракул для tests/test_fields_registry.py: реестр полей обязан давать ровно те же
ответы на всех комбинациях. Файл не правится: он снимок, а не код продукта.
"""

from __future__ import annotations

from sniffer.domain.dialogue import AnswerValue, Option, Question
from sniffer.domain.passport import Budget, Category, Currency, Passport, PassportStatus, has_value

_BUDGET_CHOICES: tuple[tuple[int, Currency], ...] = (
    (300, Currency.USD),
    (500, Currency.USD),
    (800, Currency.USD),
)
_CURRENCY_SIGN: dict[Currency, str] = {Currency.USD: "$", Currency.VND: "₫"}

LEGACY_QUESTIONS: tuple[Question, ...] = (
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
    Question(
        field="city",
        code="city",
        text="В каком городе ищем?",
        options=(Option("Нячанг", "nha_trang"), Option("Дананг", "da_nang")),
        skippable=False,
    ),
    Question(
        field="budget.max",
        code="budget",
        text="Какой бюджет? Можно написать словами — «до 400» или «до 10 млн».",
        options=tuple(
            Option(f"до {amount} {_CURRENCY_SIGN[currency]}", f"{amount} {currency.value}")
            for amount, currency in _BUDGET_CHOICES
        ),
    ),
    Question(
        field="attributes.transmission",
        code="trans",
        text="Автомат или механика?",
        options=(Option("автомат", "automatic"), Option("механика", "manual")),
    ),
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
    Question(
        field="attributes.brand",
        code="brand",
        text="Есть марка на примете?",
        options=(Option("Honda", "honda"), Option("Yamaha", "yamaha")),
    ),
    Question(
        field="attributes.rooms",
        code="rooms",
        text="Сколько комнат?",
        options=(Option("студия", "1"), Option("две", "2"), Option("три и больше", "3")),
    ),
)


def legacy_parse_option(field: str, raw: str) -> AnswerValue:
    """Значение кнопки → значение поля паспорта.

    Бюджет приезжает вместе с валютой («300 USD»), потому что подпись кнопки
    называет валюту, а значение обязано называть ту же: догадываться о ней по
    паспорту нельзя — там могут быть донги, и тогда «до 300 $» превратилось бы
    в 300 донгов.
    """
    if field == "budget.max":
        amount, _, currency = raw.partition(" ")
        return Budget(max=float(amount), currency=Currency(currency) if currency else None)
    if field == "attributes.rooms":
        return int(raw)
    return raw


# Клиент поправляет бота противопоставлением: «не 200000 VND, А обьем …»,
# «не скутер, А мотоцикл». Обе формы — из журнала бота (03.09.2026). Ключ здесь
# не отрицание само по себе («не важно» — это пропуск, а «недорого» вообще про


def legacy_apply_answer(passport: Passport, field: str, value: AnswerValue) -> Passport:
    """Ответ клиента → новая версия паспорта.

    Паспорт неизменяем (passport.md): здесь появляется новый объект, а версию
    ему присваивает репозиторий. Пропуск («не важно») сюда не доходит вовсе —
    он ничего не меняет и остаётся только событием.
    """
    update: dict[str, object] = {}
    if field == "budget.max":
        update["budget"] = _budget(passport, value)
    elif field == "category":
        update["category"] = Category.MOTORBIKE if value == "scooter" else Category(str(value))
        if value == "scooter":
            update["attributes"] = {
                **passport.attributes,
                "transmission": "automatic",
                "body_type": "tay_ga",
            }
    elif field == "city":
        update["city"] = str(value)
    elif field.startswith("attributes."):
        update["attributes"] = {**passport.attributes, field.removeprefix("attributes."): value}
    else:  # pragma: no cover — поля вне каталога вопросов сюда не приходят
        raise ValueError(f"поле {field!r} не заполняется ответом клиента")

    revised = passport.model_copy(update=update)
    return revised.model_copy(
        update={
            "missing_fields": [
                name for name in ("category", "city", "budget.max") if not has_value(revised, name)
            ],
            "status": PassportStatus.READY if revised.is_ready() else revised.status,
        }
    )


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
