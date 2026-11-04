"""Как предмет, намерение и бюджет запроса называются по-русски.

Отдельным модулем, потому что это знание нужно в двух местах сразу: в строке
«Понял: скутер, Нячанг» перед поиском и в названии поиска в меню `/requests`.
Две копии словаря разошлись бы на первой же новой категории, и разошлись бы
молча — обе выглядели бы правдой.

Лист по импортам: только `domain`. Отрисовка названий не смеет тянуть ни базу,
ни планировщик, иначе название поиска нельзя будет проверить без них.
"""

from __future__ import annotations

from sniffer.domain.districts import PLACE_BY_SLUG, ZONE_NAMES, complete_zone
from sniffer.domain.passport import RENTED_CATEGORIES, Category, Intent, Passport

# Единственное число — его читает человек, а не `passport.category.value`
# («motorbike»). Множественное живёт в заголовке выдачи (`conversation`): там
# оно согласуется с числом найденного, и это другая задача.
_NOUN: dict[Category, str] = {
    Category.MOTORBIKE: "мотобайк",
    Category.APARTMENT: "квартира",
    Category.ROOM: "комната",
    Category.HOUSE: "дом",
    Category.BICYCLE: "велосипед",
    Category.CAR: "автомобиль",
}

# Имя стороны сделки как заголовок: «Аренда: мотобайк, Нячанг». Существительное
# в именительном падеже, а не глагол («сниму»): глагол потребовал бы склонять
# предмет, а склонений в боте нет и заводить их ради подписи кнопки незачем.
_INTENT: dict[Intent, str] = {
    Intent.BUY: "Покупка",
    Intent.RENT: "Аренда",
    Intent.SELL: "Продажа",
    Intent.RENT_OUT: "Сдача",
}


def category_noun(passport: Passport) -> str:
    """Название предмета запроса. Пустая строка — категории ещё нет.

    «Скутер» — не категория, а кузов: в паспорте он живёт атрибутом
    `body_type`, потому что для рынка это тот же мотобайк. Человеку же важно
    слышать своё слово: он просил скутер, а не мотобайк.
    """
    if passport.category is None:
        return ""
    if passport.attributes.get("body_type") == "tay_ga":
        return "скутер"
    return _NOUN.get(passport.category, passport.category.value)


def intent_label(passport: Passport) -> str:
    """Сторона сделки, если она НЕ та, что подразумевает сама категория.

    Жильё в Нячанге снимают, транспорт покупают (`RENTED_CATEGORIES`) — это и есть
    молчаливое умолчание, поэтому «Квартира, Нячанг» и «Мотобайк, Нячанг» не
    несут приставки. Приставка нужна там, где умолчание нарушено: «Аренда:
    мотобайк», «Покупка: квартира». Без неё покупка и аренда одного предмета в
    одном городе давали бы две одинаковые кнопки.
    """
    implied = Intent.RENT if passport.category in RENTED_CATEGORIES else Intent.BUY
    if passport.intent is None or passport.intent is implied:
        return ""
    return _INTENT[passport.intent]


def budget_phrase(passport: Passport) -> str:
    """Display both budget bounds when the client gave a price range."""
    if passport.budget.min is None and passport.budget.max is None:
        return ""
    currency = passport.budget.currency.value if passport.budget.currency else ""

    def amount(value: float) -> str:
        return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")

    if passport.budget.min is not None and passport.budget.max is not None:
        return (
            f"от {amount(passport.budget.min)} до {amount(passport.budget.max)} {currency}".strip()
        )
    if passport.budget.min is not None:
        return f"от {amount(passport.budget.min)} {currency}".strip()
    assert passport.budget.max is not None
    return f"до {amount(passport.budget.max)} {currency}".strip()


def district_phrase(passport: Passport) -> str:
    """Name the verified selected location without claiming an unknown district."""
    if not passport.districts:
        return ""
    zone = complete_zone(passport.districts)
    if zone is not None:
        return f"район: {ZONE_NAMES[zone]}"
    names = [PLACE_BY_SLUG[slug].name for slug in passport.districts if slug in PLACE_BY_SLUG]
    return f"район: {', '.join(names)}" if names else ""


def plural(count: int, forms: tuple[str, str, str]) -> str:
    """Форма слова по числу: «1 день», «2 дня», «5 дней», «11 дней», «21 день».

    Правило склонения одно на весь бот: карточка («21 день назад»), остаток квоты
    («ещё 3 подходящих варианта») и всё, что появится дальше, берут его отсюда.
    Бот, который путает склонения, выглядит сломанным, а две копии правила
    разошлись бы молча — обе выглядели бы правдой.
    """
    one, few, many = forms
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    return few if last in (2, 3, 4) else many
