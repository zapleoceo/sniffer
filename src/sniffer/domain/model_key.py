"""Одно имя модели в разных написаниях: «PG1», «PG-1», «PG 1», «pg_1».

Паспорт хранит то, что сказал клиент, атрибут карточки — то, что извлёк парсер,
а текст объявления пишет как хочет. Раньше два фильтра (SQL-префильтр и
`matching.rules`) сравнивали эти написания по-разному и оба — строго, поэтому
«pg1» в подписке не находил «Yamaha PG-1» (листинг 435947).

Нормализация узкая намеренно: убирается разделитель только между БУКВОЙ и
следующей за ней ЦИФРОЙ («pg-1» → «pg1», «mt 15» → «mt15»). Пробел между
словами («air blade»), цифра перед буквой и граница слова не трогаются:
«pg10» остаётся другой моделью, «lead 2023» в тексте не склеивается.
Модуль без ввода-вывода — его читают и `db/`, и `matching/`.
"""

from __future__ import annotations

import re

_NON_WORD = re.compile(r"[^\w]+")
_LETTER_SPACE_DIGIT = re.compile(r"(?<=[^\W\d_]) (?=\d)")
_LETTER_DIGIT_BOUNDARY = re.compile(r"(?<=[^\W\d_])(?=\d)")


def model_words(value: str) -> str:
    """Слова модели через один пробел: регистр, `_` и пунктуация сняты."""
    return _NON_WORD.sub(" ", value.casefold().replace("_", " ")).strip()


def model_key(value: str) -> str:
    """Канонический ключ: слова, у которых «буква␣цифра» слито («pg 1» → «pg1»)."""
    return _LETTER_SPACE_DIGIT.sub("", model_words(value))


def model_text_pattern(value: str) -> str:
    """Regex для поиска модели в `model_words`-тексте: между буквой и цифрой пробел необязателен."""
    parts = _LETTER_DIGIT_BOUNDARY.split(model_key(value))
    return " ?".join(re.escape(part) for part in parts)


def model_spellings(value: str) -> frozenset[str]:
    """Написания для SQL (в нижнем регистре): исходное, слитное и с `-`/пробелом у цифры."""
    key = model_key(value)
    if not key:
        return frozenset()
    parts = _LETTER_DIGIT_BOUNDARY.split(key)
    joined = {sep.join(parts) for sep in ("", " ", "-")}
    original = value.strip().casefold()
    return frozenset(joined | {original, original.replace("_", " "), original.replace(" ", "")})
