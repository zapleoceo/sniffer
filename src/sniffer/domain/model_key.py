"""Написания одной модели для двух фильтров слежения.

Паспорт хранит то, что сказал клиент, атрибут карточки — то, что извлёк парсер,
текст объявления пишет как хочет. SQL-префильтр (`db/repositories/listings`) и
`matching.rules` обязаны считать одно и то же именем, иначе подписка `pg1` не
находит «Yamaha PG-1» (листинг 435947).

Расширяются ТОЛЬКО модели из закрытого списка `CANONICAL_ALIASES`. Для прочих
каждый гейт ведёт себя как раньше, общего правила «буква+цифра» нет намеренно:
оно задело бы чужие модели (mt15, cb1000r). Новый синоним — строка словаря и тест.
"""

from __future__ import annotations

import re

# Синонимы, которые нельзя вывести из написания. Ключ — канон, значение — все его
# написания. «pg10», «pg2», «pg-1x» сюда не входят и PG1 не совпадают.
CANONICAL_ALIASES: dict[str, frozenset[str]] = {
    "pg1": frozenset({"pg1", "pg-1", "pg 1", "pg_1"}),
}

_NON_WORD = re.compile(r"[^\w]+")


def model_words(value: str) -> str:
    """Слова через один пробел: регистр, `_` и пунктуация сняты."""
    return _NON_WORD.sub(" ", value.casefold().replace("_", " ")).strip()


def model_aliases(value: str) -> frozenset[str]:
    """Явные синонимы модели (нижний регистр) или пустой набор.

    Только для моделей из `CANONICAL_ALIASES`; у всех остальных оба фильтра ведут
    себя как раньше (SQL: исходная фраза и слитная, Python: точная фраза).
    """
    words = model_words(value)
    for aliases in CANONICAL_ALIASES.values():
        if words in {model_words(alias) for alias in aliases}:
            return aliases
    return frozenset()
