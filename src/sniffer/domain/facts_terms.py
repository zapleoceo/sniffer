"""Условия аренды из текста: залог и минимальный срок договора.

Числа здесь опаснее всего, потому что соседей у них много: «депозит 1 месяц · оплата
1 месяц · контракт от 3 месяцев» — три числа в одной строке, и каждое принадлежит
своему слову. Поэтому число читается ТОЛЬКО от своего слова и только в «месяцах»:

- залог — число сразу после «залог/депозит/deposit» («залог за один месяц», «депозит
  1+1», «Pay 3 month, deposti 2») либо перед ним («2 месяца залога»); «залог равен
  месячной аренде» — это один месяц. Сумма («залог 18 млн») месяцами не является:
  `read_deposit_months` её не читает, а `read_deposit_amount` отдаёт донгами; «без
  залога» — это 0;
- срок — число с «мес/год» после «контракт/договор/срок аренды/lease»; из «3–6
  месяцев» берётся меньшее, год — это 12. Слова оплаты между меткой и числом
  отменяют чтение: «Условия договора: депозит 1 месяц» — это залог, а не срок.

Всё читается в пределах ОДНОЙ строки: «контракт от 12 мес ⏎ депозит 2 мес» — два числа
двух слов, а не «12 месяцев депозита» (так читалось, пока пробел между числом и словом
мог быть переводом строки).

Из нескольких названных значений берётся первое: в двуязычном посте числа повторяются
(русский и английский текст подряд), и первое — то, что агентство поставило выше.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText
from sniffer.domain.price_numbers import factor, number

MAX_DEPOSIT_MONTHS, MAX_TERM_MONTHS = 24, 60
_WORDS = {
    "один": 1, "одного": 1, "одну": 1, "одной": 1, "два": 2, "две": 2, "двух": 2, "три": 3,
    "трех": 3, "четыре": 4, "четырех": 4, "шесть": 6, "шести": 6, "one": 1, "two": 2,
    "three": 3, "six": 6,
}  # fmt: skip
_WORD_ALTERNATIVES = "|".join(sorted(_WORDS, key=len, reverse=True))
_NUMBER = rf"(\d{{1,2}}(?:[.,]5)?|{_WORD_ALTERNATIVES})(?![\d]|[.,]\d)"
_MONTH = r"(?:мес\w*|months?|thang)"
_YEAR = r"(?:год\w*|лет\b|years?|nam\b)"
# Сумма, а не месяцы: «залог 18 млн», «залог 500к», «залог 5 000 000».
_MONEY_AFTER = r"\s{0,2}(?:млн|миллион\w*|trieu|tr\b|vnd|d\b|k\b|к\b|тыс\w*|usd|\$|000)"
_GAP = r"[ \t]{0,2}[:=\-–—~]?[ \t]{0,2}"

_DEPOSIT = r"(?:залог\w*|депозит\w*|депост\w*|deposit\w*|depost\w*|security|\bcoc(?![a-z]))"
_FILL = (
    r"(?:за|в размере|размер\w*|равен|равна|равный|аренд\w*|месячн\w*|плат\w*|оплат\w*|"
    r"сумм\w*|всего|of|is|amount|equals?)"
)
_DEPOSIT_AFTER = re.compile(
    rf"{_DEPOSIT}{_GAP}(?:{_FILL}[ \t]{{1,2}}){{0,4}}{_NUMBER}(?!{_MONEY_AFTER})"
)
_DEPOSIT_BEFORE = re.compile(rf"{_NUMBER}[ \t-]{{0,2}}{_MONTH}'?[ \t-]{{0,2}}{_DEPOSIT}")
# «Залог равен месячной стоимости», «в размере месячной арендной платы» — один месяц.
_EQUALS_RENT = re.compile(
    rf"{_DEPOSIT}[^\n.;]{{0,30}}?(?:равен|равна|равный|в размере|=)[^\n.;]{{0,8}}"
    r"(?:месячн\w+|ежемесячн\w+|арендн\w+[ \t]+плат\w+|"
    r"сумм\w+[ \t]+аренд\w+|стоимост\w+[ \t]+аренд\w+)"
)
_NO_DEPOSIT = re.compile(
    rf"(?:без|no|without|khong(?: co)?)[ \t]{{1,2}}{_DEPOSIT}|{_DEPOSIT}{_GAP}"
    r"(?:не\s+(?:нужен|нужна|нужно|требуется|надо)|нет\b|отсутствует|no\b|none\b)"
)
# «18M» — миллионы, но «1 m» отдельным словом — нет: поэтому `m` только вплотную к числу.
_AMOUNT_UNIT = r"млн|миллион\w*|million\w*|mln|trieu|tr\b|тыс\w*|nghin|ngan|k\b|к\b|(?<=\d)m\b"
_DEPOSIT_AMOUNT = re.compile(
    rf"{_DEPOSIT}{_GAP}(?:{_FILL}[ \t]{{1,2}}){{0,4}}"
    rf"(?P<n>\d{{1,3}}(?:[ .,]\d{{3}})+|\d{{1,3}}(?:[.,]\d{{1,2}})?)"
    rf"(?:[ \t]{{0,2}}(?P<unit>{_AMOUNT_UNIT}))?"
)
# Залог дешевле 100 тысяч донгов или дороже 200 миллионов — не залог, а чужое число.
DEPOSIT_AMOUNT_RANGE = (100_000, 200_000_000)
_MONTH_NEXT = re.compile(rf"[ \t]{{0,2}}-?[ \t]{{0,2}}{_MONTH}")

_NOT_TERM = r"(?!залог|депозит|депост|оплат|deposit|pay|coc)"
_TERM_LABEL = (
    r"(?:контракт\w*|договор\w*|срок\w*\s{1,2}(?:аренды|договора|проживания)|"
    r"минимальн\w*\s{1,2}срок|аренд\w*\s{1,2}(?:от|на)|lease|contract|"
    r"min(?:imum)?\.?\s{0,2}(?:stay|term|lease|rental)|hop dong|thoi han)"
)
_TERM_WORDS = rf"(?:{_NOT_TERM}[^\W\d_]{{1,12}}[ \t]{{0,2}}[:=\-–—~]?[ \t]{{1,2}}){{0,3}}"
_LIST_SEP = r"(?:[-–—,/]|or|и|или)(?:[ \t]{0,2}(?:or|и|или))?"
_TERM_LIST = rf"(?:[ \t]{{0,2}}{_LIST_SEP}[ \t]{{0,2}}\d{{1,2}})*"
_TERM = re.compile(
    rf"{_TERM_LABEL}{_GAP}{_TERM_WORDS}(?:>[ \t]{{0,2}})?{_NUMBER}{_TERM_LIST}"
    rf"[ \t]{{0,2}}-?[ \t]{{0,2}}(?:(?P<month>{_MONTH})|(?P<year>{_YEAR}))"
)
_HALF_YEAR = re.compile(rf"{_TERM_LABEL}\W{{0,12}}(?:от\s+)?пол(?:у)?года")


def _months(raw: str) -> float:
    return _WORDS[raw] if raw in _WORDS else float(raw.replace(",", "."))


def _whole(value: float) -> int | float:
    return int(value) if value == int(value) else value


def read_deposit_months(text: FactText) -> int | float | None:
    """Залог в месяцах аренды: 0 — «без залога», `None` — не назван или назван суммой."""
    folded = text.folded
    found: list[tuple[int, float]] = [(m.start(), 0) for m in _NO_DEPOSIT.finditer(folded)]
    found += [(m.start(), 1) for m in _EQUALS_RENT.finditer(folded)]
    for match in _DEPOSIT_BEFORE.finditer(folded):
        found.append((match.start(), _months(match.group(1))))
    for match in _DEPOSIT_AFTER.finditer(folded):
        value = _months(match.group(1))
        # Голое число после слова «залог» — месяцы, но только малое: «депозит 2» — да,
        # «депозит 10» — скорее миллионы, и угадывать мы не будем.
        named = _MONTH_NEXT.match(folded, match.end()) is not None
        if value <= (MAX_DEPOSIT_MONTHS if named else 6):
            found.append((match.start(), value))
    return _whole(min(found)[1]) if found else None


def read_min_term_months(text: FactText) -> int | float | None:
    """Минимальный срок договора в месяцах: «3–6 месяцев» → 3, «1 год» → 12."""
    found: list[tuple[int, float]] = []
    for match in _TERM.finditer(text.folded):
        value = _months(match.group(1)) * (12 if match.group("year") else 1)
        if 0 < value <= MAX_TERM_MONTHS:
            found.append((match.start(), value))
    found += [(m.start(), 6) for m in _HALF_YEAR.finditer(text.folded)]
    return _whole(min(found)[1]) if found else None


def read_deposit_amount(text: FactText) -> int | None:
    """Залог суммой в донгах: «депозит 18 млн» → 18_000_000; `None` — суммой не назван.

    Число без единицы читается только записанным группами («5 000 000»): голое «2» после
    «залога» — месяцы, их читает `read_deposit_months`. Доллары не читаются: курс —
    чужое знание, а залог в долларах в этих чатах называют редко.
    """
    for match in _DEPOSIT_AMOUNT.finditer(text.folded):
        unit = match.group("unit")
        raw = match.group("n")
        if unit is None and re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", raw) is None:
            continue
        amount = int(number(raw) * (factor(unit) if unit else 1))
        if DEPOSIT_AMOUNT_RANGE[0] <= amount <= DEPOSIT_AMOUNT_RANGE[1]:
            return amount
    return None
