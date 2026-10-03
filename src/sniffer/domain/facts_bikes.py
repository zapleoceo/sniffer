"""Факты мотобайка из текста объявления: год, пробег, документы, «права не нужны», торг.

У байка из паспорта карточка уже несёт марку, модель, объём, коробку и тип двигателя —
их читает разбор запроса (`search.intake_rules`), и воронка кладёт их в атрибуты. Здесь
остальное из `CATEGORY_ATTRIBUTES[MOTORBIKE]` и из того, о чём в постах пишут чаще всего
(замер 03.10.2026, 2051 пост продажи): год назван в 69%, документы — в 50%, «права не
нужны» — в 20%, торг — в 22%, пробег — в 38%.

Каждое число читается с меткой, потому что у байка чисел много: «до 100 км от города»
— не пробег, «до 60 км/ч» — не пробег, «2 000 000» — не год:

- год — «2019 год», «2012г», «Год: 2021» или голое «2013» в первых двух строках, там, где
  стоит название лота; голый год из подвала («updated 2026») годом выпуска не считается;
- пробег — только после слова «пробег»/«mileage»; «55к» и «15 тыс.» — тысячи;
- торг — слово «торг» целиком: «торговый центр» — не торг, а «без торга» — наоборот.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText

YEAR_MIN, YEAR_MAX = 1985, 2030
MAX_MILEAGE_KM = 300_000
_YEAR = r"(?<![\d.,/:-])((?:19[89]\d|20[0-3]\d))(?![\d]|[.,]\d)"
_STRONG_YEAR = (
    re.compile(rf"{_YEAR}[ \t]{{0,2}}(?:г\b|год\w*|year|yr\b|model|модел)"),
    re.compile(
        r"(?:год(?: выпуска| производства| первой регистрации)?|year(?: of manufacture)?|"
        rf"model year|nam san xuat|doi|yom)[ \t]{{0,2}}[:=\-–—]?[ \t]{{0,2}}\(?{_YEAR}"
    ),
)
# Голый год в названии лота: после него не должно идти слово, делающее число чем-то иным.
_BARE_YEAR = re.compile(
    rf"{_YEAR}(?![ \t]{{0,2}}(?:км|km|usd|vnd|₫|k\b|к\b|тыс|cc|сс|см|кг|kg|%|мес|шт|руб))"
)
_TITLE_LINES = 2
# «Куплен в мае 2026 года» — дата покупки, а не год выпуска.
_MONTH_BEFORE = re.compile(
    r"(?:январ|феврал|март|апрел|ма[йея]|июн|июл|август|сентябр|октябр|ноябр|декабр|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*[ \t]{0,2}$"
)

# Скорость («60 км/ч») — не пробег. Проверка стоит СРАЗУ после числа: если отложить её за
# единицу, «км» проглотится и «/ч» останется за кадром.
_MILEAGE = re.compile(
    r"(?:пробег\w*|mileage|odo(?:meter)?|da di|so km)[^\d\n]{0,25}?"
    r"(?P<num>\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d)?)(?![ \t]{0,2}(?:км/ч|km/h|км в час|км/час))"
    r"[ \t]{0,2}(?P<unit>км|km|тыс\w*|тысяч\w*|к\b|k\b)?"
)

_PAPERS_NONE = re.compile(
    r"без\s+(?:документ\w*|блю\w*|blue)|документ\w*\s+(?:нет|отсутствуют|потеряны|утеряны)|"
    r"no\s+(?:blue\s?card|papers|documents)|khong\s+(?:co\s+)?(?:cavet|giay to|blue\s?card)"
)
_PAPERS_YES = re.compile(
    r"blue\s?-?card|bluecard|блю\s?-?кар\w*|блу\s?-?кар\w*|син(?:яя|ей)\s+карт\w*|cavet|"
    r"техпаспорт|\bптс\b|\bpapers\b|giay to day du|"
    r"документ\w*[ \t]{0,2}[:\-–—]?[ \t]{0,2}"
    r"(?:в порядке|все|есть|на руках|в наличии|полн\w*|оригинал\w*)|"
    r"(?:все|полный комплект|с)\s+документ\w*"
)

# «Права не нужны» говорят в любом порядке и с чем угодно между: «не нужны водительские
# права», «права и шлем не нужны», «на них не нужны права», «права: не нужны».
_LICENSE_WORD = r"(?:пра?ва\w*|пава|водительск\w*[ \t]+прав\w*)"
_NOT_NEEDED = r"(?:не[ \t]?нужн\w*|ненужн\w*|не[ \t]+треб\w*|не[ \t]+надо|не[ \t]+обязательн\w*)"
_NO_LICENSE = re.compile(
    rf"{_LICENSE_WORD}[^\n.,;]{{0,25}}?{_NOT_NEEDED}|{_NOT_NEEDED}[^\n.,;]{{0,25}}?{_LICENSE_WORD}|"
    r"(?:можно|даже|ездить|доступны)[ \t]+без[ \t]+прав\w*|\bбез[ \t]+прав\b|"
    r"no[ \t]+licen[sc]e[ \t]+(?:needed|required|necessary)|"
    r"licen[sc]e[ \t]+(?:is[ \t]+)?not[ \t]+(?:needed|required)|"
    r"without[ \t]+(?:a[ \t]+)?licen[sc]e|khong[ \t]+can[ \t]+bang"
)
_LICENSE_BAN = re.compile(r"нельзя|штраф|запрещ|illegal|fine\b")

_BARGAIN_NO = re.compile(
    r"без\s+торг\w*|торг\w*\s+(?:нет|не\s+(?:уместен|предусмотрен|возможен|нужен))|не\s+торгу\w*|"
    r"цена\s+(?:окончательная|фиксированная|последняя)|fixed\s+price|no\s+(?:bargain\w*|negotiat\w*|haggl\w*)|"
    r"non-negotiable|khong\s+thuong\s+luong|gia\s+co\s+dinh"
)
_BARGAIN_YES = re.compile(
    r"\bторг(?:а|у|е|ом|и)?\b|торгу\w*|торговать\w*|bargain\w*|negotiab\w*|negotiat\w*|"
    r"haggl\w*|thuong luong"
)


def _year_of(match: re.Match[str], text: str) -> int | None:
    """Год из совпадения, если он правдоподобен и не стоит после названия месяца."""
    year = int(match.group(1))
    before = text[max(0, match.start(1) - 14) : match.start(1)]
    return year if YEAR_MIN <= year <= YEAR_MAX and not _MONTH_BEFORE.search(before) else None


def read_year(text: FactText) -> int | None:
    """Год выпуска: подтверждённый единицей или меткой лучше голого в названии лота."""
    head = "\n".join(text.folded.splitlines()[:_TITLE_LINES])
    candidates = [
        (m, text.folded) for pattern in _STRONG_YEAR for m in pattern.finditer(text.folded)
    ]
    candidates += [(m, head) for m in _BARE_YEAR.finditer(head)]
    return next((year for match, source in candidates if (year := _year_of(match, source))), None)


def read_mileage_km(text: FactText) -> int | None:
    """Пробег в километрах — только после слова «пробег» и не больше разумного предела."""
    for match in _MILEAGE.finditer(text.folded):
        raw, unit = match.group("num"), match.group("unit") or ""
        grouped = re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", raw) is not None
        value = float(re.sub(r"[ .,]", "", raw)) if grouped else float(raw.replace(",", "."))
        thousands = unit.startswith("тыс") or unit in ("к", "k")
        km = round(value * 1000) if thousands else round(value)
        if (unit or km >= 1000) and 0 < km <= MAX_MILEAGE_KM:
            return km
    return None


def _papers(folded: str) -> str | None:
    """«Нет блю карт» содержит «блю карт»: слова внутри отрицания не считаются «есть»."""
    refused = [m.span() for m in _PAPERS_NONE.finditer(folded)]
    present = any(
        not any(start <= m.start() and m.end() <= end for start, end in refused)
        for m in _PAPERS_YES.finditer(folded)
    )
    if bool(refused) == present:
        return None
    return "none" if refused else "blue_card"


def _no_license(folded: str) -> bool | None:
    for match in _NO_LICENSE.finditer(folded):
        clause = folded[max(0, match.start() - 25) : match.end() + 25]
        if not _LICENSE_BAN.search(clause):
            return True
    return None


def _bargain(folded: str) -> str | None:
    fixed = [m.span() for m in _BARGAIN_NO.finditer(folded)]
    free = [
        m for m in _BARGAIN_YES.finditer(folded)
        if not any(start <= m.start() and m.end() <= end for start, end in fixed)
    ]  # fmt: skip
    if bool(fixed) == bool(free):
        return None
    return "fixed" if fixed else "negotiable"


def bike_facts(text: FactText, *, deal_type: str) -> dict[str, object]:
    """Документы и «права не нужны» — у любого предложения; год, пробег и торг — у продажи.

    В прокате пост описывает парк байков, а не один лот: год и пробег там — чьи именно?
    """
    facts: dict[str, object] = {}
    if (papers := _papers(text.folded)) is not None:
        facts["papers"] = papers
    if _no_license(text.folded):
        facts["no_license_claimed"] = True
    if deal_type != "sell":
        return facts
    if (year := read_year(text)) is not None:
        facts["year"] = year
    if (mileage := read_mileage_km(text)) is not None:
        facts["mileage_km"] = mileage
    if (bargain := _bargain(text.folded)) is not None:
        facts["bargain"] = bargain
    return facts
