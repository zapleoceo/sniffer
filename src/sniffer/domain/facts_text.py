"""Текст объявления, подготовленный для чтения фактов.

Фактов из одного поста читает несколько модулей — жильё, байки, район, заголовок,
язык, — и каждому нужен один и тот же очищенный текст в двух видах. Готовить его
заново в каждом — значит держать три копии правил очистки, и однажды поправят две.

Что убирается и почему:

- цифры-эмодзи, «жирные» цифры и скрытые знаки — `clean_text`, как у цены;
- ссылки и хэштеги: в `#нячанг #аренда #p1520` нет описания лота, зато есть слова,
  которые читаются как факты («#oceanus» — не район, а тег агентства);
- строки-меню агентств («КВАРТИРЫ И СТУДИИ», «АРЕНДА В ЖК ОКЕАНУС», «HOUSE IN
  DANANG»): это навигация по ЧУЖИМ объявлениям. Меню AN-HOME стоит в подвале 1437
  карточек из 18 868 и без этого дарило каждой «студию», «Океанус» и животных
  (замер 03.10.2026);
- сверх предела — текст и отдельная строка: воронка обрабатывает сообщения по
  одному, и простыня из четырёх тысяч знаков не вправе её остановить.

Два вида текста нужны потому, что вьетнамские слова пишут и с диакритикой, и без
неё, а «tặng» (подарок) без диакритики — «tang» (этаж). Слова-омографы читаются по
тексту с диакритикой (`low`), остальные — по свёрнутому (`folded`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sniffer.domain.facts_vocab import MENU_LINE_RE
from sniffer.domain.text_clean import clean_text

MAX_TEXT = 6_000
MAX_LINE = 600
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")


def _fold_table() -> dict[int, str]:
    """Латиница без диакритики, кириллица нетронута (иначе «й» стала бы «и»)."""
    table: dict[int, str] = {}
    for code in range(0xC0, 0x1EFA):
        base = unicodedata.normalize("NFD", chr(code))[0]
        if base.isascii() and base.isalpha():
            table[code] = base
    table[ord("đ")] = "d"
    table[ord("Đ")] = "D"
    return table


_FOLD = _fold_table()


def fold(text: str) -> str:
    """Строчные буквы, вьетнамские слова без диакритики, «ё» как «е»."""
    return text.translate(_FOLD).casefold().replace("ё", "е")


@dataclass(frozen=True, slots=True)
class FactText:
    """Очищенные строки поста и два вида его текста для регулярных выражений."""

    lines: tuple[str, ...]
    low: str
    folded: str


def fact_text(text: str) -> FactText:
    """Подготовить пост: очистить, убрать меню, теги и ссылки, обрезать по пределам."""
    cleaned = _HASHTAG_RE.sub(" ", _URL_RE.sub(" ", clean_text(text[:MAX_TEXT])))
    kept = [
        line[:MAX_LINE]
        for line in cleaned.splitlines()
        if re.search(r"\w", line) and not MENU_LINE_RE.match(fold(line))
    ]
    body = "\n".join(kept)
    return FactText(tuple(kept), body.casefold(), fold(body))
