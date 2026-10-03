"""Язык объявления — ru, en или vi — по письму и частотным словам, без модели и сети.

У Telegram-архива язык не ставился вообще (`listings.lang` пуст у всех 18 880 карточек),
а нужен он карточке и фильтру («только по-русски»). Определение детерминированное и
дешёвое: считаются буквы по алфавитам.

- кириллица: русскоязычная часть — от четверти букв. Двуязычный пост «русский +
  английский» (так пишет большинство агентств) — русский: клиент читает его по-русски;
- вьетнамская диакритика у латиницы: вьетнамский текст несёт её на каждом третьем слове,
  а английский с названием улицы («Nguyễn Thiện Thuật») — на единицах, поэтому порог доли;
- вьетнамский без диакритики («cho thue can ho 2 phong ngu») узнаётся по частотным
  словам: слов вьетнамских больше, чем английских, и их не меньше трёх;
- без единой буквы (одни цифры и значки) языка нет — `None`, а не угадывание.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import fold
from sniffer.domain.text_clean import clean_text

MAX_TEXT = 6_000
RUSSIAN_SHARE = 0.25
MIN_RUSSIAN_LETTERS = 12
VIETNAMESE_MARK_SHARE = 0.05
VIETNAMESE_DENSE_SHARE = 0.15
RUSSIAN_DOMINANT_SHARE = 0.4
MIN_VIETNAMESE_WORDS = 3

_NOISE_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+|[#@]\w+")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
# Латиница — и расширенная (вьетнамская). Диапазон «à-ỹ» нельзя: он накрывает и кириллицу.
_LATIN_RE = re.compile(r"[a-z\U000000c0-\U0000024f\U00001e00-\U00001eff]", re.IGNORECASE)
# Буквы, которые есть только во вьетнамском письме: Ă Â Đ Ê Ô Ơ Ư и всё с тоновым знаком.
_VIETNAMESE_RE = re.compile(
    r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE
)
_VIETNAMESE_WORDS = re.compile(
    r"\b(?:cho thue|can ho|phong ngu|nha|gia|thang|dien|nuoc|tang|day du|noi that|tien ich|"
    r"lien he|moi|dep|rong|thoang|khong|co|duoc|va|voi|tai|gan|bien)\b"
)
_ENGLISH_WORDS = re.compile(
    r"\b(?:the|for|rent|and|with|bedroom|apartment|furnished|available|price|month|deposit|"
    r"is|in|to|of|house|room|near|beach|contact)\b"
)


def detect_lang(text: str) -> str | None:
    """Язык поста: `ru`, `en` или `vi`; `None` — в тексте нет ни одной буквы."""
    body = _NOISE_RE.sub(" ", clean_text(text[:MAX_TEXT]))
    cyrillic, latin = len(_CYRILLIC_RE.findall(body)), len(_LATIN_RE.findall(body))
    if cyrillic + latin == 0:
        return None
    marked = len(_VIETNAMESE_RE.findall(body)) / max(latin, 1)
    share = cyrillic / (cyrillic + latin)
    # Вьетнамский пост с короткой русской строкой агрегатора («Цена: По запросу»): латиница
    # там густо с диакритикой, а кириллицы меньше трети — это вьетнамский, не русский.
    mostly_vietnamese = marked >= VIETNAMESE_DENSE_SHARE and share < RUSSIAN_DOMINANT_SHARE
    if cyrillic >= MIN_RUSSIAN_LETTERS and share >= RUSSIAN_SHARE and not mostly_vietnamese:
        return "ru"
    if cyrillic > latin:
        return "ru"
    folded = fold(body)
    vietnamese, english = (
        len(_VIETNAMESE_WORDS.findall(folded)),
        len(_ENGLISH_WORDS.findall(folded)),
    )
    # Диакритика без перевеса вьетнамских слов — английский текст с вьетнамскими названиями
    # («1-BEDROOM APARTMENT FOR RENT – MỸ ĐA ĐÔNG 12»), а не вьетнамский.
    if marked >= VIETNAMESE_MARK_SHARE and vietnamese >= english:
        return "vi"
    return "vi" if vietnamese >= MIN_VIETNAMESE_WORDS and vietnamese > english else "en"
