"""Ответ на уточняющий вопрос словами вместо кнопки.

Кнопки — быстрый путь, но клиент пишет «до 400» или «механика» ровно так же
часто, как нажимает. Не понять свой же вопрос — худший способ выглядеть
роботом, поэтому свободный ввод разбирается тем же знанием, что и первичный
запрос (`intake_rules`), а не отдельным набором правил.

Почему это `search/`, а не `domain/`: здесь знание о том, какими словами
клиент формулирует ответ, — то же самое знание, что и в разборе запроса.
Домен решает, какой вопрос задать и что сделать с ответом; слова разбираются
здесь.
"""

from __future__ import annotations

import re

from sniffer.domain.dialogue import AnswerValue
from sniffer.search.budget_rules import parse_budget
from sniffer.search.intake_rules import detect_brand, detect_category, detect_transmission
from sniffer.search.rooms import read_rooms

# «Не важно» в любом виде. Кнопка есть, но нажимают не всегда: половина людей
# отвечает словами, и «да пофиг» обязано означать то же, что нажатие.
_SKIP_RE = re.compile(
    r"\b(?:не\s*важно|неважно|не\s*принципиально|всё\s*равно|все\s*равно|пофиг"
    r"|без\s*разницы|люб(?:ой|ая|ое|ые)|как\s*получится|покажи|показывай|давай\s*что\s*есть"
    r"|any|whatever|skip|don'?t\s*care)\b",
    re.IGNORECASE,
)

# Коробки здесь нет: её слова живут в словаре рынка и читаются оттуда
# (`intake_rules.detect_transmission`). Свой список стоял тут и успел разъехаться
# со словарём — «xe số» значило в нём механику, а в словаре полуавтомат, — и
# заметить это по тексту было нельзя: оба списка выглядели правдой.

# Состояние остаётся здесь: у клиента для него свои слова («убитый пойдёт»),
# которых в словаре ПРОДАВЦА нет и быть не должно. Это не то же дублирование —
# знание разное, а не текст.
_CONDITION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("new", re.compile(r"\b(?:нов\w*|new)\b", re.IGNORECASE)),
    ("good", re.compile(r"\b(?:хорош\w*|отличн\w*|good|ухожен\w*)\b", re.IGNORECASE)),
    (
        # «лишь бы ездил» и «на ходу» — это подпись кнопки `worn` («любой, лишь
        # бы ездил») и её разговорные варианты. Без них `is_skip` съедал бы
        # «любой» в начале фразы, и дословно набранная подпись кнопки означала
        # бы пропуск вопроса вместо ответа на него.
        "worn",
        re.compile(
            r"\b(?:убит\w*|потр[её]пан\w*|worn|под\s*восстановлен\w*"
            r"|лишь\s*бы\s*(?:ездил\w*|ехал\w*|бегал\w*|на\s*ходу)|на\s*ходу)\b",
            re.IGNORECASE,
        ),
    ),
)

# Число комнат читает общее знание `search.rooms` — оно же на стороне запроса и
# лота. Здесь остаётся ТОЛЬКО голое и словесное число («2», «две», «три»),
# безопасное лишь потому, что вопрос «сколько комнат?» уже задан: в запросе и
# тексте лота такое число это что угодно (год, цена), а в ОТВЕТЕ — комнаты.
# Разговорные «студия», «двушка» и «2 спальни» читает `read_rooms`, их тут нет.
_FEEDBACK_ROOMS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"\b(?:одна|одну|1)\b", re.IGNORECASE)),
    (2, re.compile(r"\b(?:две|двух\w*|2)\b", re.IGNORECASE)),
    (3, re.compile(r"\b(?:три|тр[её]х\w*|3)\b", re.IGNORECASE)),
)


def is_skip(text: str) -> bool:
    """Клиент сказал «не важно» словами, а не кнопкой."""
    return bool(_SKIP_RE.search(text))


def interpret(field: str, text: str) -> AnswerValue | None:
    """Слова клиента → значение поля. `None` — «это не ответ на вопрос».

    `None` важнее, чем кажется: на вопрос про бюджет клиент нередко отвечает
    новым запросом («ладно, тогда квартиру»), и принять его за сумму значит
    потерять запрос.
    """
    if field == "budget.max":
        budget = parse_budget(text)
        return budget if budget.max else None
    if field == "category":
        category = detect_category(text)
        return category.value if category else None
    if field == "attributes.brand":
        return detect_brand(text)
    if field == "attributes.transmission":
        return detect_transmission(text)
    if field == "attributes.condition":
        return _match(_CONDITION_RULES, text)
    if field == "attributes.rooms":
        return _rooms(text)
    return None


def _match(rules: tuple[tuple[str, re.Pattern[str]], ...], text: str) -> str | None:
    for value, pattern in rules:
        if pattern.search(text):
            return value
    return None


def _rooms(text: str) -> int | None:
    """Число комнат из ответа: общее знание плюс голое/словесное число.

    `read_rooms` покрывает «студию», «двушку», «2 спальни»; голое «2» и «две»
    добираются здесь — они комната только потому, что уже спрошено, сколько их.
    """
    explicit = read_rooms(text)
    if explicit is not None:
        return explicit
    for value, pattern in _FEEDBACK_ROOMS:
        if pattern.search(text):
            return value
    return None
