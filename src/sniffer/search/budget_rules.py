"""Бюджет из формулировки клиента: «до 400 долларов», «до 10 млн донгов».

Отдельным файлом от остального разбора: деньги — единственное место, где
разбор действительно нетривиален. Рынок Нячанга двухвалютный, и клиент
называет сумму то в долларах, то в миллионах донгов, то вовсе без валюты.
"""

from __future__ import annotations

import re

from sniffer.domain.passport import Budget, Currency, Intent, PricePeriod

# Множители пишут цифрами, словом и по-вьетнамски: «400к», «10 млн», «10tr»,
# «10 triệu».
_MULTIPLIERS: dict[str, int] = {
    "k": 1_000,
    "к": 1_000,
    "тыс": 1_000,
    "m": 1_000_000,
    "млн": 1_000_000,
    "миллион": 1_000_000,
    "лям": 1_000_000,
    "tr": 1_000_000,
    "triệu": 1_000_000,
}

# Пробел внутри числа только обычный и неразрывный: `\s` пустил бы перенос
# строки и склеил два числа из соседних строк в одно. Хвост `\w*` живёт внутри
# необязательной группы: он дописывает окончание множителю («миллиона»,
# «тысяч»), но не смеет съесть слово после числа — иначе «2019 года» не
# отличить от суммы.
# `(?<!\w)` — число обязано начинать токен, а не сидеть в хвосте слова. Число,
# склеенное с буквой, это код модели, а не сумма: живой отказ 02.09.2026 —
# «Kawasaki z300» дал бюджет «до 300 USD», и клиент, искавший 300-кубовый
# мотоцикл, получил 50cc. `z300`, `cbr250`, `mt15` — номер в имени модели; пробел
# решает: «до 300» сумма, «z300» нет. `\w` (а не только буква) в запрете
# обязателен: иначе из «cbr250» прочиталось бы «50» — хвост, стоящий за цифрой.
_AMOUNT_RE = re.compile(
    r"(?<!\w)"
    r"(?P<num>\d[\d\u00a0 .,]*\d|\d)\s*(?:(?P<mult>k|к|тыс|млн|миллион|лям|tr|triệu|m)\w*)?",
    re.IGNORECASE,
)

# Маркер стоит перед суммой: «до 400», «от 300». Смотрим короткое окно слева,
# иначе «до» из соседнего предложения приклеится к чужому числу.
_MARKER_WINDOW = 24
_UPPER_RE = re.compile(
    r"\b(?:до|не\s+больше|не\s+дороже|максимум|макс|в\s+пределах|under|up\s+to|max)"
    r"\s*[~≈]?\s*$",
    re.IGNORECASE,
)
_LOWER_RE = re.compile(
    r"\b(?:от|не\s+меньше|не\s+дешевле|from|min)\s*[~≈]?\s*$",
    re.IGNORECASE,
)

_CURRENCIES: tuple[tuple[Currency, re.Pattern[str]], ...] = (
    (Currency.USD, re.compile(r"\$|\busd\b|доллар|долл|бакс|у\.?\s?е\.?", re.IGNORECASE)),
    (Currency.VND, re.compile(r"₫|\bvnd\b|донг|\bđ\b", re.IGNORECASE)),
    (Currency.EUR, re.compile(r"€|\beur\b|евро", re.IGNORECASE)),
    (Currency.RUB, re.compile(r"₽|\brub\b|руб|рубл", re.IGNORECASE)),
)

# Срок аренды в паспорт кладётся сюда, как период цены: «посуточно» — day,
# «длительный срок»/«надолго» — month (норма аренды, оно же по умолчанию для
# RENT). Отдельного атрибута `term` заводить не стали: период уже есть в схеме
# (`Budget.period`), и «посуточно» это ровно «цена за сутки».
_PERIODS: tuple[tuple[PricePeriod, re.Pattern[str]], ...] = (
    (
        PricePeriod.DAY,
        re.compile(
            r"в\s?(?:сутки|день)|на\s?сутки|/\s?(?:сут|день)|per\s+day|посуточн|\bdaily\b",
            re.IGNORECASE,
        ),
    ),
    (PricePeriod.WEEK, re.compile(r"в\s?неделю|/\s?нед|per\s+week|недельн", re.IGNORECASE)),
    (
        PricePeriod.MONTH,
        re.compile(
            r"в\s?месяц|/\s?мес|per\s+month|месячн|ежемесяч"
            r"|длительн|надолго|долгосроч|long[\s-]?term",
            re.IGNORECASE,
        ),
    ),
)

# Год выпуска и цена пишутся одинаково — четырьмя цифрами. Различает их только
# слово рядом, поэтому «2019 года» суммой не считаем.
_YEAR_RE = re.compile(r"^\s*(?:год|г\.|г\b|year|гв)", re.IGNORECASE)
_YEAR_RANGE = range(1990, 2036)

# Число, за которым идёт СЧЁТНАЯ единица (комнаты, срок аренды), а не денежная,
# — не сумма. «квартиру 2 спальни» → это две спальни, не «до 2 USD»; «на 3 дня»,
# «на 3 месяца» → это срок, не «до 3». Тот же приём, что у года («2019 года»):
# цену и не-цену различает слово ПОСЛЕ числа. Проверяется только у множителя 1 —
# у «15 млн» множитель не 1, поэтому «2 спальни до 15 млн» оставляет 15 млн
# бюджетом, а «2» отбрасывает. Денежных слов (доллар, донг, млн, $) в списке нет
# намеренно: за ними число — как раз сумма.
_NON_MONEY_AFTER_RE = re.compile(
    r"[\s-]*(?:"
    r"спал(?:ьн|ен)\w*|комнат\w*|бедрум\w*|спален"  # комнаты (ru)
    r"|дн\w*|день|сут\w*|недел\w*|мес\b|месяц\w*|лет|год\w*"  # срок (ru)
    r"|bedroom\w*|bed\b|br\b|room\w*"  # комнаты (en)
    r"|day\w*|week\w*|month\w*|year\w*"  # срок (en)
    r")",
    re.IGNORECASE,
)

# Ниже этого порога сумма в местной валюте бессмысленна: 400 донгов не бывает,
# а 400 долларов бывает каждый день. Порог заодно решает «до 10 млн» без
# валюты — миллионами здесь считают только донги.
_VND_THRESHOLD = 100_000


def parse_budget(text: str, *, intent: Intent | None = None) -> Budget:
    """Что клиент готов заплатить. Ничего не нашли — пустой бюджет, не ноль."""
    amounts = _amounts(_without_rejected(text))
    lows = [value for value, mark in amounts if mark == "low"]
    highs = [value for value, mark in amounts if mark == "high"]
    plain = [value for value, mark in amounts if mark is None]

    if not highs and plain:
        # «300-500$» — вилка; одинокое число это потолок, а не пожелание снизу.
        highs = [max(plain)]
        if not lows and len(plain) > 1:
            lows = [min(plain)]

    return Budget(
        min=min(lows) if lows else None,
        max=max(highs) if highs else None,
        currency=_currency(text) or _guess_currency(highs + lows),
        period=_period(text, intent),
    )


# Клиент поправляет бота: «не 200000 VND, а объём двигателя». Число после «не»
# — то, от чего он ОТКАЗЫВАЕТСЯ, и читать его как бюджет значит повторить
# ровно ту ошибку, которую человек только что вслух исправил.
#
# Живой случай 01.09.2026: бот принял «200 кубиков» за 200000 VND, клиент
# написал «не 200000 VND, а обьем мощность двигателя до 200 кубических
# сантиметров» — и получил тот же бюджет 200000 VND второй раз. Поправка,
# которую система не слышит, хуже первой ошибки: первая — недоразумение,
# вторая — ощущение, что тебя не слушают.
_REJECTED_RE = re.compile(
    r"(?:^|[^а-яё])"
    r"не\s+(?:по\s+)?"
    r"\d[\d\s.,]*\s*"
    r"(?:[$€₫]|usd|eur|vnd|đ|донг\w*|доллар\w*|млн|k|к)?",
    re.IGNORECASE,
)


def _without_rejected(text: str) -> str:
    """Убрать суммы, которые клиент прямо отверг словом «не»."""
    return _REJECTED_RE.sub(" ", text)


def _amounts(text: str) -> list[tuple[float, str | None]]:
    found: list[tuple[float, str | None]] = []
    for match in _AMOUNT_RE.finditer(text):
        multiplier = _MULTIPLIERS.get((match.group("mult") or "").lower(), 1)
        number = _to_number(match.group("num"))
        if number is None:
            continue
        value = number * multiplier
        if value <= 0:
            continue
        if multiplier == 1 and int(value) in _YEAR_RANGE and _YEAR_RE.match(text[match.end() :]):
            continue
        if multiplier == 1 and _NON_MONEY_AFTER_RE.match(text[match.end() :]):
            continue
        found.append((value, _marker(text[: match.start()])))
    return found


def _marker(before: str) -> str | None:
    window = before[-_MARKER_WINDOW:]
    if _UPPER_RE.search(window):
        return "high"
    if _LOWER_RE.search(window):
        return "low"
    return None


def _to_number(raw: str) -> float | None:
    """Число из захваченной строки — или None, если арифметике оно не поддаётся.

    None, а не исключение: `parse_query` зовётся на КАЖДОМ сообщении рынка и
    тексте клиента и падать на произвольном вводе не вправе. Регексп суммы жадный
    и хватает почти-числа — живой отказ 02.09.2026: «13.000.0002» (лишняя цифра в
    группе разрядов) ронял `float()`, а с ним весь разбор и воркер на этом
    сообщении. Такую сумму просто не считаем названной — это честнее краха.
    """
    text = raw.replace(" ", "").replace("\u00a0", "")
    # «5.000.000» и «5,000,000» — разделители тысяч, а «1,5 млн» — дробь.
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        return float(re.sub(r"[.,]", "", text))
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _currency(text: str) -> Currency | None:
    for currency, pattern in _CURRENCIES:
        if pattern.search(text):
            return currency
    return None


def _guess_currency(values: list[float]) -> Currency | None:
    """Валюту не назвали — судим по порядку суммы."""
    if not values:
        return None
    return Currency.VND if max(values) >= _VND_THRESHOLD else Currency.USD


def _period(text: str, intent: Intent | None) -> PricePeriod:
    for period, pattern in _PERIODS:
        if pattern.search(text):
            return period
    # Аренду называют помесячно почти всегда, покупку — разово всегда.
    return PricePeriod.MONTH if intent in (Intent.RENT, Intent.RENT_OUT) else PricePeriod.ONCE
