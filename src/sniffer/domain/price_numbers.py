"""Числа суммы: как читаются «9 000 000», «9tr5», «19.5 vnd» и сколько это в донгах.

Только арифметика и запись числа, без контекста: что вокруг суммы и считать ли её
ценой, решает `price_facts.py`.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from sniffer.domain.price_vocab import UNIT_FACTORS

# 10 млрд донгов — около 380 тысяч долларов. Дороже в Нячанге не продают ни
# байков, ни квартир; потолок закреплён тестом после падения воронки 01.09.2026.
MAX_PLAUSIBLE_VND = 10_000_000_000

# «15 000 ₫» — не цена байка и не аренда квартиры: это либо надбавка, либо
# «15 тысяч», написанное неряшливо. Суточная аренда байка начинается от
# 100 тысяч, так что порог ниже живых цен.
MIN_PLAUSIBLE_VND = 50_000
USD_RANGE = (20, 1_000_000)

# Число длиннее этого — номер, счёт или спам, а не цена. Без предела `float` на
# строке из четырёх тысяч девяток даёт бесконечность, и `int()` роняет воронку
# на одном сообщении (найдено прогоном 4-килобайтных текстов, 03.10.2026).
MAX_DIGITS = 18

# Вьетнамская запись «9tr5» — 9,5 миллиона, «16tr50» — 16,5, «2tr500» — 2,5.
# Левая граница обязательна: без неё серия из шести тысяч цифр пробовалась с
# каждой позиции и разбор занимал четверть секунды (замер Opus 03.10.2026).
_COMPACT_RE = re.compile(r"(?<!\d)(\d{1,18})\s*(tr|triệu|trieu)(\d{1,3})(?!\d)", re.IGNORECASE)
_GROUPED_RE = re.compile(r"\d{1,3}(?:[ .,]\s?\d{3})+")
# После хвоста без единицы сумма кончается: конец строки, знак препинания, валюта.
# «15 млн 500 метров», «25 млн 100 км», «21 млн 200 cc» — это не 15,5 млн, а слово
# за числом; «9 млн 500 000» — не хвост, а ещё одно число.
_ENDS_THE_SUM_RE = re.compile(r"\s*(?:$|[^\w\s]|(?:vnd|vnđ|₫|đ|dong|донг)(?![\w]))", re.IGNORECASE)
# Число перед тире — не начало вилки, а номер, если сумма за тире больше него
# во столько раз: «Yamaha NVX 125 — 4 200 000 ₫». Вилки такого размаха не пишут.
_INDEX_RATIO = 50
# «2 — 13 млн», «Ха Куанг 2 – 35 млн»: тире с пробелами и сумма втрое больше —
# это «номер — цена», а не «от 2 до 13». Вилку «3–10 млн» пишут без пробелов.
_SPACED_INDEX_RATIO = 3


def expand_compact(text: str) -> str:
    """«9tr5» → «9.5 tr»: так число читается обычным разбором."""

    def spelled(match: re.Match[str]) -> str:
        return f"{match.group(1)}.{match.group(3).rstrip('0') or '0'} {match.group(2)}"

    return _COMPACT_RE.sub(spelled, text)


def factor(unit: str | None) -> int:
    key = (unit or "").casefold()
    return next((value for prefix, value in UNIT_FACTORS if key.startswith(prefix)), 1)


def number(raw: str) -> float:
    """«9 000 000», «9.000.000» — тысячи; «9.5», «9,5» — десятичная дробь."""
    if _GROUPED_RE.fullmatch(raw):
        # `\s`, а не пробел: разделителем групп бывает и таб, и U+1680, и U+001F
        # (регулярка числа пускает любой пробельный знак после точки). Прежняя
        # чистка знала только пробел, и «1.\t000.000» роняла воронку на `float`.
        return float(re.sub(r"[\s.,]", "", raw))
    return float(raw.replace(",", "."))


def currency_code(written: str | None) -> str | None:
    """``VND``, ``USD``, ``OTHER`` (рубли, евро: пересчитать нечем) или ``None``."""
    if not written:
        return None
    low = written.casefold()
    if low in {"₫", "đ", "vnđ", "vnd", "внд", "d", "ď"} or low.startswith(("dong", "донг")):
        return "VND"
    return "USD" if low in {"$", "usd"} or low.startswith(("дол", "у")) else "OTHER"


def _tail(match: re.Match[str]) -> float:
    """Хвост составной суммы в единицах главной: «2 tỷ 300 triệu» → 0,3, «4 миллиона 500» → 0,5.

    Вьетнамцы и русские пишут крупную сумму двумя частями. Хвост без единицы —
    это следующий разряд, и только круглый трёхзначный («500», «300»): «15 млн 2
    комнаты» — не 15,002. Хвост с единицей обязан быть мельче главной: «15 млн 500 м
    от моря» — метры, а не 515 миллионов.
    """
    raw = match.group("tail")
    if raw is None:
        return 0.0
    main = factor(match.group("unit"))
    if match.group("tail_unit") is not None:
        small = factor(match.group("tail_unit"))
    elif (
        main in (10**6, 10**9)
        and len(raw) == 3
        and int(raw) % 50 == 0
        and _ENDS_THE_SUM_RE.match(match.string, match.end("tail"))
    ):
        small = main // 1000
    else:
        return 0.0
    return int(raw) * small / main if small < main else 0.0


def _is_index(match: re.Match[str], low: float, high: float) -> bool:
    """Число перед тире — номер, площадь или модель, а не начало вилки цен.

    «№402 — 9 500 000» — не диапазон от 402: живой промах 03.10.2026, десять
    квартир-строк одного объявления читались как «от 402 до 9 500 000» и
    пропадали. Тем же признаком отсеяны хвост слова («35 м2 — 12 млн» — это «2»
    из «м2»), порядки разницы и «номер — цена» через тире с пробелами. А вилка со
    словом «до» («от 4 до 15 млн») или с тире без пробелов («3–10 млн») — вилка
    при любом размахе: прежнее правило «верх втрое больше низа» читало её как
    «15 млн» и теряло «от 4». Вилка не идёт вниз: «Yamaha NVX 155 — 25 млн» — это
    модель и цена, а не «155 млн» (прежний разбор брал первое число с единицей
    второго).
    """
    text, start = match.string, match.start("lo")
    if high <= low or (start > 0 and text[start - 1].isalnum()):
        return True
    if high > _INDEX_RATIO * low:
        return True
    link = text[match.end("lo") : match.start("hi")]
    # Только длинное тире: «2 — 13 млн», «Ха Куанг 2 – 35 млн». Дефис с пробелами
    # («5 - 20 млн») пишут вилкой.
    spaced_dash = link.strip() in {"—", "–"} and link[:1].isspace() and link[-1:].isspace()
    return spaced_dash and high > _SPACED_INDEX_RATIO * low


class Figure(NamedTuple):
    """Число суммы в строке: значение, верх вилки, где запись начинается и где кончается."""

    low: float
    high: float | None
    start: int
    end: int


def numbers(match: re.Match[str]) -> Figure | None:
    """Число суммы, верх вилки и границы записи в строке; ``None`` — не число цены."""
    if any(len(match.group(name) or "") > MAX_DIGITS for name in ("lo", "hi")):
        return None
    low = number(match.group("lo"))
    tail = _tail(match) if not match.group("hi") else 0.0
    # Хвост, который не стал частью суммы («15 млн 2 комнаты»), в записи не показываем.
    end = match.end() if tail or match.group("tail") is None else match.end("unit")
    if not match.group("hi"):
        return Figure(low + tail, None, match.start(), end)
    high = number(match.group("hi"))
    if _is_index(match, low, high):
        return Figure(high, None, match.start("hi"), end)
    return Figure(low, high, match.start(), end)


def scale(unit: str | None, written: str | None, code: str | None, value: float) -> int:
    """Множитель суммы: единица, а без единицы у донгов меньше тысячи — миллионы."""
    if unit is None and code == "VND" and value < 1000 and (written or "").casefold() != "d":
        return 10**6  # «19.5 vnd» и «18 vnd» — миллионы: восемнадцати донгов не бывает
    return factor(unit)


def plausible(amount: int, code: str) -> bool:
    if code == "USD":
        return USD_RANGE[0] <= amount <= USD_RANGE[1]
    return MIN_PLAUSIBLE_VND <= amount <= MAX_PLAUSIBLE_VND
