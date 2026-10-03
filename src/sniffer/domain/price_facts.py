"""Суммы объявления: что в тексте похоже на цену предмета и чем это подтверждено.

Это не источник и не extractor: одно и то же знание нужно живому Telegram
поиску и обработке накопленного архива. Число признаём ценой только в
подходящем контексте, иначе год, пробег, ``125cc``, телефон и, главное, соседние
суммы становятся ложными донгами.

Сумм в объявлении о жилье много, и почти все они не арендная плата: залог,
электричество за кВт, вода с человека, управление, интернет, парковка, надбавка
за питомца. Замер 03.10.2026 на 700 квартирах без извлечённой цены: чаще всего
рядом с метками стояли именно такие суммы, а сама аренда — на отдельной строке
или в итоговой строке агрегатора. Взять «первую сумму» или «наименьшую из всех»
значило бы показать клиенту цену воды. Поэтому у каждой суммы смотрим контекст
и записываем, чем она подтверждена; выбирает между ними `prices.choose_price`.

Метка не обязательна: «14M VND/month» после значка денег, «36 млн.» отдельной
строкой и «Honda Vision за 15 млн» — всё это цены. Метка нужна лишь числу без
единицы и валюты («Цена 7 500 000»): без неё это год, пробег или код объявления.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sniffer.domain.price_vocab import (
    AMOUNT_RE,
    BOUNDARY_RE,
    COUNT_AFTER_RE,
    DIRECT_LABEL_RE,
    FOOTER_RE,
    HEADER_LABEL_RE,
    LABEL_RE,
    MONEY,
    OTHER_AFTER_RE,
    OTHER_BEFORE_RE,
    PERIOD_AFTER,
    PERIOD_BEFORE,
    SURCHARGE_RE,
    UNIT_FACTORS,
    UPTO_RE,
    WEAK_LABEL_RE,
    WEAK_UNITS,
)
from sniffer.domain.text_clean import clean_text

# 10 млрд донгов — около 380 тысяч долларов. Дороже в Нячанге не продают ни
# байков, ни квартир; столько же держит колонка `NUMERIC(14,2)`.
MAX_PLAUSIBLE_VND = 10_000_000_000

# «15 000 ₫» — не цена байка и не аренда квартиры: это либо надбавка, либо
# «15 тысяч», написанное неряшливо. Суточная аренда байка начинается от
# 100 тысяч, так что порог ниже живых цен.
MIN_PLAUSIBLE_VND = 50_000
USD_RANGE = (20, 1_000_000)
# Телефон — число без единицы и валюты, и цену из него не сделать: у цены нет
# ведущего нуля («0905 123 456»), а «84905123456» с кодом страны больше потолка
# для чисел без единицы (`_bare_number`).
_LETTERS_RE = re.compile(r"[^\W\d_]")
_NOISE_RE = re.compile(r"#\w+|<[^>]*>")
_DIGIT_RE = re.compile(r"\d")
# Вьетнамская запись «9tr5» — 9,5 миллиона, «16tr50» — 16,5, «2tr500» — 2,5.
_COMPACT_RE = re.compile(r"(\d+)\s*(tr|triệu|trieu)(\d{1,3})(?!\d)", re.IGNORECASE)
_LABELED = frozenset({"label", "weak"})


@dataclass(frozen=True, slots=True)
class PriceFact:
    """Цена из текста: сколько, в чём, за какой срок и чем подтверждена.

    ``source`` — сила пометки: ``label`` («Цена:», «Аренда»), ``money`` (значок
    денег перед суммой), ``text`` (сумма без пометки), ``footer`` (итоговая
    строка бота-агрегатора). ``bare`` — число без единицы и валюты, слишком
    малое для донгов («Цена 19.500»): ``value`` хранит его как написано, а масштаб
    выбирают границы правдоподобия.
    """

    raw: str
    amount: int
    currency: str
    period: str | None
    source: str
    up_to: int | None = None
    value: float = 0.0
    bare: bool = False


def _factor(unit: str | None) -> int:
    key = (unit or "").casefold()
    return next((factor for prefix, factor in UNIT_FACTORS if key.startswith(prefix)), 1)


def _number(raw: str) -> float:
    """«9 000 000», «9.000.000» — тысячи; «9.5», «9,5» — десятичная дробь."""
    if re.fullmatch(r"\d{1,3}(?:[ .,]\s?\d{3})+", raw):
        return float(re.sub(r"[ .,]", "", raw))
    return float(raw.replace(",", "."))


def _currency_code(written: str | None) -> str | None:
    if not written:
        return None
    low = written.casefold()
    if low in {"₫", "đ", "vnđ", "vnd", "внд", "d", "ď"} or low.startswith(("dong", "донг")):
        return "VND"
    return "USD" if low in {"$", "usd"} or low.startswith(("долл", "у")) else "OTHER"


def _split(pre: str) -> tuple[str, str]:
    """Значок-граница перед суммой и текст между ним и суммой."""
    last = None
    for last in BOUNDARY_RE.finditer(pre):  # noqa: B007 -- нужен последний
        pass
    return (last.group(0)[0], pre[last.end() :]) if last else ("", pre)


def _is_alone(line: str, match: re.Match[str]) -> bool:
    rest = _NOISE_RE.sub("", line[: match.start()] + line[match.end() :])
    return _LETTERS_RE.search(rest) is None


def _source(line: str, lead: str, segment: str, *, carried: bool) -> str:
    if FOOTER_RE.match(line):
        return "footer"
    bare = not segment.strip(" :—–-")
    if LABEL_RE.search(segment) or (bare and carried):
        return "label"
    if WEAK_LABEL_RE.search(segment):
        return "weak"
    return "money" if bare and lead in MONEY else "text"


def _period(segment: str, post: str) -> str | None:
    for name, pattern in PERIOD_AFTER:
        if pattern.search(post):
            return name
    return next((name for name, pattern in PERIOD_BEFORE if pattern.search(segment)), None)


def _numbers(match: re.Match[str]) -> tuple[float, float | None, int]:
    """Число суммы, верх диапазона и позиция, с которой суммa начинается в строке.

    «№402 — 9 500 000» — не диапазон: первое число там номер, и «до» у него
    втрое больше «от». Живой промах 03.10.2026: десять квартир-строк одного
    объявления читались как «от 402 до 9 500 000» и пропадали.
    """
    low = _number(match.group("lo"))
    if not match.group("hi"):
        return low, None, match.start()
    high = _number(match.group("hi"))
    if high > 3 * low:
        return high, None, match.start("hi")
    return low, (high if high > low else None), match.start()


def _scale(unit: str | None, written: str | None, code: str | None, value: float) -> int:
    factor = _factor(unit)
    if unit is None and code == "VND" and value < 1000 and (written or "").casefold() != "d":
        factor = 10**6  # «19.5 vnd» и «18 vnd» — миллионы: восемнадцати донгов не бывает
    return factor


def _plausible(amount: int, code: str) -> bool:
    if code == "USD":
        return USD_RANGE[0] <= amount <= USD_RANGE[1]
    return MIN_PLAUSIBLE_VND <= amount <= MAX_PLAUSIBLE_VND


def _bare_number(
    match: re.Match[str], amount: int, source: str, post: str, *, alone: bool, direct: bool
) -> str | None:
    """Число без единицы и валюты: ``price``, ``small`` (цена в сокращении) или ``None``.

    Не год, не этаж, не телефон и не номер: без метки порог выше, потому что
    нечем отличить цену от «2025». «Цена 19.500» у байка и «Цена 8.5» у квартиры —
    сокращённые тысячи и миллионы: сам по себе такой ответ не поймёшь, и его
    масштаб выбирают границы правдоподобия (`prices.choose_price`), но только
    если метка стоит вплотную: «договор на 3 месяца» — не три миллиона. Срок
    сразу за числом («7,500,000/month») делает его ценой и без метки.
    """
    digits = re.sub(r"\D", "", match.group("lo"))
    if digits.startswith("0") or COUNT_AFTER_RE.match(post):
        return None
    periodic = _period("", post) is not None
    if source == "text" and not (alone or periodic):
        return None
    if (1_000_000 if source == "text" and not periodic else 100_000) <= amount < 2_000_000_000:
        return "price"
    return "small" if direct and amount < 100_000 else None


def _rejected(line: str, match: re.Match[str], segment: str, post: str, code: str | None) -> bool:
    """Контекст говорит, что это не цена предмета: сбор, залог, надбавка, потолок запроса.

    Слова до суммы смотрим лишь после последней метки цены: в строке без знаков
    препинания «…пробег 21к цена 21млн» пробег относится к «21к», а не к цене.
    """
    label = LABEL_RE.search(segment)
    nearby = segment[label.start() :] if label else segment[-80:]
    return (
        code == "OTHER"
        or OTHER_BEFORE_RE.search(nearby) is not None
        or OTHER_AFTER_RE.match(post) is not None
        or SURCHARGE_RE.search(line[: match.start()]) is not None
        or UPTO_RE.search(segment) is not None
    )


def _unit_ok(
    unit: str | None, code: str | None, source: str, *, alone: bool, period: object
) -> bool:
    """Слабая единица («35 m», «500 ml», «130к») — деньги лишь при подтверждении."""
    if not unit or unit.casefold() not in WEAK_UNITS:
        return True
    if unit.casefold() == "ml" and code is None and source == "text":
        return False
    return bool(code or source != "text" or alone or period)


def _fact(line: str, match: re.Match[str], *, carried: bool) -> PriceFact | None:
    lead, segment = _split(line[: match.start()])
    post = line[match.end() : match.end() + 60]
    unit, written = match.group("unit"), match.group("cur") or match.group("pcur")
    code = _currency_code(written)
    if _rejected(line, match, segment, post, code):
        return None
    source = _source(line, lead, segment, carried=carried)
    alone, period = _is_alone(line, match), _period(segment, post)
    if not _unit_ok(unit, code, source, alone=alone, period=period):
        return None
    low, high, start = _numbers(match)
    factor = _scale(unit, written, code, low)
    amount = int(low * factor)
    bare = False
    if unit is None and code is None:
        direct = (
            source == "money"
            or DIRECT_LABEL_RE.search(segment) is not None
            or (source == "label" and not segment.strip(" :—–-"))
        )
        verdict = _bare_number(match, amount, source, post, alone=alone, direct=direct)
        if verdict is None:
            return None
        bare = verdict == "small"
    elif unit is None and code == "VND" and amount < MIN_PLAUSIBLE_VND:
        # «Цена: 6.500 ₫» у скутера — это 6,5 миллиона: продавцы опускают тысячи.
        # Масштаб, как у числа без валюты, выбирают границы правдоподобия.
        bare = source != "text" or alone
    code = code or "VND"
    if not (bare or _plausible(amount, code)):
        return None
    label = (
        (LABEL_RE.search(segment) or WEAK_LABEL_RE.search(segment)) if source in _LABELED else None
    )
    raw = (segment[label.start() :] if label else "") + line[start : match.end()]
    return PriceFact(
        raw.strip(), amount, code, period, source, int(high * factor) if high else None, low, bare
    )


def _expand_compact(text: str) -> str:
    """«9tr5» → «9.5 tr»: вьетнамская запись «девять с половиной миллионов»."""

    def spelled(match: re.Match[str]) -> str:
        return f"{match.group(1)}.{match.group(3).rstrip('0') or '0'} {match.group(2)}"

    return _COMPACT_RE.sub(spelled, text)


def parse_prices(text: str) -> list[PriceFact]:
    """Все суммы текста, которые похожи на цену самого предмета объявления.

    Метка над списком («💰 Цены:») относится ко всем строкам списка, пока каждая
    из них содержит цену: цены по этажам и срокам идут именно так.
    """
    found: list[PriceFact] = []
    carried = False
    for line in _expand_compact(clean_text(text)).splitlines():
        here = [
            fact
            for match in AMOUNT_RE.finditer(line)
            if (fact := _fact(line, match, carried=carried)) is not None
        ]
        found.extend(here)
        stripped = BOUNDARY_RE.sub(" ", line).strip()
        if stripped and not here:
            # Метка над списком — строка без цифр: «Цены:», «Условия аренды:».
            # Строка с цифрами, чью сумму отбросил контекст («Плата за
            # управление: 700 000»), метки списку не даёт.
            carried = HEADER_LABEL_RE.search(stripped) is not None and not _DIGIT_RE.search(
                stripped
            )
    return found
