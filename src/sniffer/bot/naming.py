"""Как предмет, намерение и бюджет запроса называются по-русски.

Отдельным модулем, потому что это знание нужно в двух местах сразу: в строке
«Понял: скутер, Нячанг» перед поиском и в названии поиска в меню `/requests`.
Две копии словаря разошлись бы на первой же новой категории, и разошлись бы
молча — обе выглядели бы правдой.

Лист по импортам: только `domain`. Отрисовка названий не смеет тянуть ни базу,
ни планировщик, иначе название поиска нельзя будет проверить без них.
"""

from __future__ import annotations

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
    """«до 400 USD», «до 10 000 000 VND». Пустая строка — потолка цены нет."""
    if not passport.budget.max:
        return ""
    currency = passport.budget.currency.value if passport.budget.currency else ""
    amount = f"{passport.budget.max:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"до {amount} {currency}".strip()
