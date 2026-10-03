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

Цена контекста ограничена: окно слева 200 знаков, справа 60, не больше 60 сумм
на строку и 6000 знаков на текст. Без этого строка из тысячи чисел считалась
секунды, а воронка обрабатывает сообщения по одному.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import islice

from sniffer.domain.price_numbers import (
    MIN_PLAUSIBLE_VND,
    currency_code,
    expand_compact,
    numbers,
    plausible,
    scale,
)
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
    UPTO_RE,
    WEAK_LABEL_RE,
    WEAK_UNITS,
)
from sniffer.domain.text_clean import clean_text

_WINDOW = 200
_MAX_TEXT = 6_000
_MAX_PER_LINE = 60
_LETTERS_RE = re.compile(r"[^\W\d_]")
_NOISE_RE = re.compile(r"#\w+|<[^>]*>")
_DIGIT_RE = re.compile(r"\d")
_LABELED = frozenset({"label", "weak"})


@dataclass(frozen=True, slots=True)
class PriceFact:
    """Цена из текста: сколько, в чём, за какой срок и чем подтверждена.

    ``source`` — сила пометки: ``label`` («Цена:», «Аренда»), ``weak`` (метка и
    слова без двоеточия), ``money`` (значок денег перед суммой), ``text`` (сумма
    без пометки), ``footer`` (итоговая строка бота-агрегатора). ``bare`` — число
    без единицы и валюты, слишком малое для донгов («Цена 19.500»): ``value``
    хранит его как написано, а масштаб выбирают границы правдоподобия.
    """

    raw: str
    amount: int
    currency: str
    period: str | None
    source: str
    up_to: int | None = None
    value: float = 0.0
    bare: bool = False


@dataclass(frozen=True, slots=True)
class _Around:
    """Окрестность суммы в строке: что стоит до неё и после, чем она подтверждена."""

    before: str
    lead: str
    segment: str
    post: str
    source: str
    alone: bool
    period: str | None


def _split(before: str) -> tuple[str, str]:
    """Значок-граница перед суммой и текст между ним и суммой."""
    last = None
    for last in BOUNDARY_RE.finditer(before):  # noqa: B007 -- нужен последний
        pass
    return (last.group(0)[0], before[last.end() :]) if last else ("", before)


def _period(segment: str, post: str) -> str | None:
    for name, pattern in PERIOD_AFTER:
        if pattern.search(post):
            return name
    return next((name for name, pattern in PERIOD_BEFORE if pattern.search(segment)), None)


def _source(line: str, lead: str, segment: str, *, carried: bool) -> str:
    if len(line) <= _WINDOW and FOOTER_RE.match(line):
        return "footer"
    bare = not segment.strip(" :—–-")
    if LABEL_RE.search(segment) or (bare and carried):
        return "label"
    if WEAK_LABEL_RE.search(segment):
        return "weak"
    return "money" if bare and lead in MONEY else "text"


def _is_alone(line: str, match: re.Match[str]) -> bool:
    """Строка — одна сумма и ничего, кроме значков, хэштегов и ссылок."""
    if len(line) > _WINDOW:
        return False
    rest = _NOISE_RE.sub("", line[: match.start()] + line[match.end() :])
    return _LETTERS_RE.search(rest) is None


def _around(line: str, match: re.Match[str], *, carried: bool) -> _Around:
    before = line[max(0, match.start() - _WINDOW) : match.start()]
    lead, segment = _split(before)
    post = line[match.end() : match.end() + 60]
    source = _source(line, lead, segment, carried=carried)
    return _Around(
        before, lead, segment, post, source, _is_alone(line, match), _period(segment, post)
    )


def _rejected(ctx: _Around, code: str | None) -> bool:
    """Контекст говорит, что это не цена предмета: сбор, залог, надбавка, потолок запроса.

    Слова до суммы смотрим лишь после последней метки цены: в строке без знаков
    препинания «…пробег 21к цена 21млн» пробег относится к «21к», а не к цене.
    """
    label = LABEL_RE.search(ctx.segment)
    nearby = ctx.segment[label.start() :] if label else ctx.segment[-80:]
    return (
        code == "OTHER"
        or OTHER_BEFORE_RE.search(nearby) is not None
        or OTHER_AFTER_RE.match(ctx.post) is not None
        or SURCHARGE_RE.search(ctx.before) is not None
        or UPTO_RE.search(ctx.segment) is not None
    )


def _unit_ok(unit: str | None, code: str | None, ctx: _Around) -> bool:
    """Слабая единица («35 m», «500 ml», «130к») — деньги лишь при подтверждении."""
    if not unit or unit.casefold() not in WEAK_UNITS:
        return True
    if unit.casefold() == "ml" and code is None and ctx.source == "text":
        return False
    return bool(code or ctx.source != "text" or ctx.alone or ctx.period)


def _bare_number(match: re.Match[str], amount: int, ctx: _Around) -> str | None:
    """Число без единицы и валюты: ``price``, ``small`` (цена в сокращении) или ``None``.

    Не год, не этаж, не телефон и не номер: без метки порог выше, потому что
    нечем отличить цену от «2025». «Цена 19.500» у байка и «Цена 8.5» у квартиры —
    сокращённые тысячи и миллионы: сам по себе такой ответ не поймёшь, и его
    масштаб выбирают границы правдоподобия (`prices.choose_price`), но только
    если метка стоит вплотную: «договор на 3 месяца» — не три миллиона. Срок
    сразу за числом («7,500,000/month») делает его ценой и без метки.
    """
    digits = re.sub(r"\D", "", match.group("lo"))
    if digits.startswith("0") or COUNT_AFTER_RE.match(ctx.post):
        return None
    periodic = _period("", ctx.post) is not None
    if ctx.source == "text" and not (ctx.alone or periodic):
        return None
    if (1_000_000 if ctx.source == "text" and not periodic else 100_000) <= amount < 2_000_000_000:
        return "price"
    direct = (
        ctx.source == "money"
        or DIRECT_LABEL_RE.search(ctx.segment) is not None
        or (ctx.source == "label" and not ctx.segment.strip(" :—–-"))
    )
    return "small" if direct and amount < 100_000 else None


def _fact(line: str, match: re.Match[str], *, carried: bool) -> PriceFact | None:
    ctx = _around(line, match, carried=carried)
    unit, written = match.group("unit"), match.group("cur") or match.group("pcur")
    code = currency_code(written)
    parts = numbers(match)
    if parts is None or _rejected(ctx, code) or not _unit_ok(unit, code, ctx):
        return None
    low, high, start = parts
    factor = scale(unit, written, code, low)
    amount = int(low * factor)
    bare = False
    if unit is None and code is None:
        verdict = _bare_number(match, amount, ctx)
        if verdict is None:
            return None
        bare = verdict == "small"
    elif unit is None and code == "VND" and amount < MIN_PLAUSIBLE_VND:
        # «Цена: 6.500 ₫» у скутера — это 6,5 миллиона: продавцы опускают тысячи.
        # Масштаб, как у числа без валюты, выбирают границы правдоподобия.
        bare = ctx.source != "text" or ctx.alone
    code = code or "VND"
    if not (bare or plausible(amount, code)):
        return None
    label = (
        (LABEL_RE.search(ctx.segment) or WEAK_LABEL_RE.search(ctx.segment))
        if ctx.source in _LABELED
        else None
    )
    raw = (ctx.segment[label.start() :] if label else "") + line[start : match.end()]
    up_to = int(high * factor) if high else None
    return PriceFact(raw.strip(), amount, code, ctx.period, ctx.source, up_to, low, bare)


def parse_prices(text: str) -> list[PriceFact]:
    """Все суммы текста, которые похожи на цену самого предмета объявления.

    Метка над списком («💰 Цены:») относится ко всем строкам списка, пока каждая
    из них содержит цену: цены по этажам и срокам идут именно так.
    """
    found: list[PriceFact] = []
    carried = False
    for line in expand_compact(clean_text(text[:_MAX_TEXT])).splitlines():
        here = [
            fact
            for match in islice(AMOUNT_RE.finditer(line), _MAX_PER_LINE)
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
