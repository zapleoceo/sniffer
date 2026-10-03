"""Заголовок карточки: первая СОДЕРЖАТЕЛЬНАЯ строка поста, а если её нет — строка из фактов.

Раньше заголовком становилась первая непустая строка, и 14% заголовков были мусором
(замер 03.10.2026): «AN-HOME» — 1437 карточек из 18 868, «#нячанг #аренда #сдам» — 486,
ряды эмодзи, «📱 https://…», рекламные «Свободна и готова к заселению!». Клиент видел в
выдаче десять карточек с одним и тем же словом вместо названия.

Строка не заголовок, если в ней (после срезания украшений по краям) нет слов, а только:

- хэштеги, упоминания, ссылки, эмодзи и значки;
- одно слово без цифр в другом слове — бренд агентства («AN-HOME», «LVCC», «OCEANUS»), метка;
- цена: «14 000 000 VND/мес» — это не название, а следующее поле карточки;
- «служебное»: «Свободна с 8 октября», «Снимите дом без комиссии», «Мебель: полностью»,
  рамка объявления без самого лота («Квартира в аренду», «2 спальни», «Нячанг центр»).

Если содержательной строки нет, заголовок строится из фактов: «Студия · 35 м² · Север
Нячанга». Длина — не больше 80 знаков, обрезка по слову: заголовок читают в списке из
десяти карточек, и тут нет места пересказу поста.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from sniffer.domain.districts import ZONE_NAMES
from sniffer.domain.text_clean import clean_text

MAX_TITLE = 80
MIN_CUT = 30
SCAN_LINES = 6
MAX_TEXT = 6_000
NO_TITLE = "Объявление"

_SYMBOLS = re.compile(r"[\U00002190-\U00002bff\U0001f000-\U0001faff\U0000fe0f\U0000200d\U000020e3]")
_URL = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
# Телефон в заголовок не попадает: контакты в карточке не хранятся, в ней стоит ссылка на
# оригинал. Номер начинается с +, 0 или 84 — а цена («15 000 000», «12.000.000») нет.
_PHONE = re.compile(r"(?<![\w.])(?:\+\d|0\d|84)[\d ().\-]{7,}\d(?!\w)")
_WORD = re.compile(r"[^\W\d_]{2,}")
# Рамка объявления без лота: все слова строки — из этого набора (числа и состав — тоже).
_FRAME_WORDS = (
    r"\d+|сда\w*|аренд\w*|в|на|квартир\w*|апартамент\w*|студи\w*|дом|комнат\w*|вилл\w*|нячанг\w*|"
    r"вьетнам|спальн\w*|ванн\w*|санузл\w*|центр\w*|север\w*|юг\w*|юж\w*|запад\w*|город\w*|"
    r"for|rent|apartment\w*|house|studio|room|villa|in|nha|trang|vietnam|bedrooms?|bathrooms?|br"
)
_FRAME = re.compile(rf"(?:{_FRAME_WORDS})(?:\s+(?:{_FRAME_WORDS}))*", re.IGNORECASE)
# Строка «ключ: значение» из списка характеристик — не название лота.
_SPEC_KEYS = (
    r"цена|стоимость|площадь|район|локация|расположение|адрес|код|id|контакт\w*|залог|депозит|"
    r"оплата|контракт|договор|срок|мебель|удобства|планировка|описание|условия|этаж|вид|"
    r"состояние|пробег|цвет|тип|здание|комплекс|вода|электричество|интернет|управление|просмотр|"
    r"менеджмент|парковка|price|area|location|address|deposit|payment|contract|lease|"
    r"furniture|layout|floor|view|type|details|features|amenities|contact"
)
# Служебные фразы агентств: доступность лота, рекламные баннеры, заголовки разделов поста,
# расстояние до моря («5 минут до моря») — не название, а поле карточки или реклама.
_SERVICE = re.compile(
    "|".join(
        (
            r"(?:свободн(?:а|о|ы)|свободен|доступн(?:а|о|ы)|доступен|готов(?:а|о|ы)?)\b"
            r"[:!?.,]*(?:\s+\S+){0,6}|освободил\w+[:!?.,]*(?:\s+\S+){0,2}",
            r"available(?:\s+\S+){0,3}|vacant|\d+/\d+\s+свобод\w*|с\s+\d[\d./]*",
            r"планируем\w+\s+дат\w+.*|можно\s+(?:с\s+животными|посмотреть|заселиться|заехать).*",
            r"есть\s+вопросы.*|аренда\s+начинается\s+здесь|снимите\s+\S+(?:\s+\S+){0,5}",
            r"приоритет\b.*|без\s+комисси\w*|полностью\s+меблирован\w*.*|\d+\s+кондиционер\w*",
            r"(?:дополнительн|коммунальн|характеристик|условия|контакт|отзыв)\w*(?:\s+\S+){0,2}",
            r"(?:google\s+maps|faq|медицина|трансфер|оплата|на\s+карте)",
            r"\d+\s*(?:мин\w*|метр\w*|м)\s+(?:пешком\s+)?до\s+\S+",
            r"[\d.,\s]+(?:донг\w*|vnd|vnđ|₫|млн|миллион\w*|k|к)\b.*",
            rf"(?:{_SPEC_KEYS})\s*[:\-–—].*",
        )
    ),
    re.IGNORECASE,
)
_PRICE_WORDS = frozenset(
    "млн миллион миллионов vnd vnđ донг донгов донгах месяц мес month per only price цена всего "
    "triệu trieu from квтч квт kwh".split()
)
_STUDIO = re.compile(r"студи|studio", re.IGNORECASE)
_CATEGORY_WORDS = {
    "apartment": "Квартира", "room": "Комната", "house": "Дом", "motorbike": "Мотобайк",
    "bicycle": "Велосипед", "car": "Авто",
}  # fmt: skip


def _trim(line: str) -> str:
    """Без знаков по краям, но «(сутки/месяц)» не теряет закрывающую скобку."""
    line = re.sub(r"^[\W_]+", "", line)
    while line and not (
        line[-1].isalnum()
        or line[-1] in "%+"
        or (line[-1] == ")" and line.count("(") >= line.count(")"))
    ):
        line = line[:-1]
    return line


def _cleaned(raw: str) -> str:
    """Строка без ссылок, телефонов, эмодзи, тегов и украшений по краям; пробелы схлопнуты."""
    tokens = _SYMBOLS.sub(" ", _PHONE.sub(" ", _URL.sub(" ", raw))).split()
    return _trim(" ".join(token for token in tokens if token[0] not in "#@"))


def _is_content(line: str) -> bool:
    tokens = line.split()
    words = [token for token in tokens if _WORD.search(token)]
    if not words:
        return False
    # Одно слово — бренд или метка, кроме «Студия 30м2»: число в ДРУГОМ слове лота.
    if len(words) == 1 and not any(any(c.isdigit() for c in t) for t in tokens if t not in words):
        return False
    if all(run.casefold() in _PRICE_WORDS for run in _WORD.findall(line)):
        return False
    return not (_FRAME.fullmatch(line) or _SERVICE.fullmatch(line))


def _cut(line: str) -> str:
    """Не длиннее `MAX_TITLE`; обрезка по слову, без повисшей пунктуации."""
    if len(line) <= MAX_TITLE:
        return line
    head = line[: MAX_TITLE - 1]
    space = head.rfind(" ")
    return _trim(head[:space] if space >= MIN_CUT else head) + "…"


def content_title(text: str) -> str | None:
    """Первая содержательная строка поста или `None`, если все строки — рамка и украшения."""
    lines = clean_text(text[:MAX_TEXT]).splitlines()[:SCAN_LINES]
    return next((_cut(line) for raw in lines if _is_content(line := _cleaned(raw))), None)


def _kind(category: str, attributes: Mapping[str, object], text: str) -> str:
    rooms = attributes.get("rooms")
    if category == "motorbike":
        name = " ".join(
            str(attributes[key]).replace("_", " ").title()
            for key in ("brand", "model")
            if attributes.get(key)
        )
        return name or _CATEGORY_WORDS[category]
    if category == "apartment":
        studio = _STUDIO.search("\n".join(text.splitlines()[:3])) and rooms in (None, 1)
        if studio:
            return "Студия"
        return f"{rooms}-комн. квартира" if isinstance(rooms, int) else "Квартира"
    if category == "house" and isinstance(rooms, int):
        return f"Дом, {rooms} спальни"
    return _CATEGORY_WORDS.get(category, "")


def facts_title(
    category: str,
    attributes: Mapping[str, object],
    text: str,
    *,
    place_name: str | None,
    zone: str | None,
) -> str | None:
    """Заголовок из фактов: вид · площадь или год · место. `None` — фактов нет."""
    parts = [_kind(category, attributes, text)]
    for key, unit in (("area_m2", " м²"), ("year", "")):
        value = attributes.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            parts.append(f"{value:g}{unit}")
    parts.append(place_name or ZONE_NAMES.get(zone or "", ""))
    title = " · ".join(part for part in parts if part)
    # Одно общее слово («Квартира») без единого факта — не заголовок: лучше «Объявление».
    return None if title in ("", _CATEGORY_WORDS.get(category, "")) else title


def listing_title(
    text: str,
    category: str,
    attributes: Mapping[str, object],
    *,
    place_name: str | None = None,
    zone: str | None = None,
) -> str:
    """Заголовок карточки: содержательная строка поста, иначе факты, иначе «Объявление»."""
    title = content_title(text)
    return (
        title
        or facts_title(category, attributes, text, place_name=place_name, zone=zone)
        or NO_TITLE
    )
