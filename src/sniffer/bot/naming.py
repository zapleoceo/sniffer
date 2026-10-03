"""Как предмет запроса называется по-русски.

Отдельным модулем, потому что это знание нужно в двух местах сразу: в строке
«Понял: скутер, Нячанг» перед поиском и в заголовке ветки в меню `/requests`.
Две копии словаря разошлись бы на первой же новой категории, и разошлись бы
молча — обе выглядели бы правдой.

Лист по импортам: только `domain`. Отрисовка названий не смеет тянуть ни базу,
ни планировщик, иначе заголовок ветки нельзя будет проверить без них.
"""

from __future__ import annotations

from sniffer.domain.passport import Category, Passport

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
