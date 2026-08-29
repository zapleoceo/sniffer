"""Ступень 1 воронки: бесплатный regex-гейт.

Отсекает ~85–90% сообщений до того, как хоть одно уйдёт в LLM. Без этой
ступени 6000 сообщений в день превращаются в 6000 вызовов модели, и никакой
free-квоты не хватает.

Гейт СРАЗУ мультиязычный: чаты Нячанга русско-англо-вьетнамские, и гейт
только на русском выкосит половину настоящих объявлений.

Пороги и паттерны здесь — стартовая эвристика. Их надо перетюнить на реальной
выборке: `reason` у отклонённых сообщений для того и возвращается.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sniffer.domain.passport import Category

# 300$ · $300 · 300 usd · 300к · 7 triệu · 7tr · 5.000.000 vnd · 5 млн · 300 у.е.
PRICE_RE = re.compile(
    r"""
    (?: [$€₫]\s?\d[\d\s.,]* )
  | (?: \d[\d\s.,]*\s?(?:\$|€|₫|usd|eur|vnd|đ|rub|руб) )
  | (?: \d[\d.,]*\s?(?:k|к|tr|triệu|млн|тыс|m)\b )
  | (?: \d{3}[\s.,]\d{3} )
  | (?: \d[\d\s.,]*\s?(?:у\.?е\.?|долл|бакс) )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Предложение: кто-то что-то отдаёт.
OFFER_RE = re.compile(
    r"\b(?:прода(?:м|ю|ется|ётся|жа)|отда(?:м|ю)|сда(?:м|ю|ется|ётся)|аренда|в\s?аренду"
    r"|for\s?sale|selling|sell|for\s?rent|renting|available|to\s?let"
    r"|bán|cho\s?thuê|cần\s?bán)\b",
    re.IGNORECASE,
)

# Спрос: человек ищет. Это не предложение — в listings такое не идёт.
DEMAND_RE = re.compile(
    r"\b(?:ищу|ищем|куплю|сниму|подскажите\s+где|нужен|нужна|нужно"
    r"|looking\s+for|want\s+to\s+(?:buy|rent)|wanted|need"
    r"|tìm|cần\s?mua|cần\s?thuê)\b",
    re.IGNORECASE,
)

CATEGORY_HINTS: dict[Category, re.Pattern[str]] = {
    Category.MOTORBIKE: re.compile(
        r"\b(?:байк|скутер|мотоцикл|мопед|honda|yamaha|suzuki|piaggio|vision|air\s?blade"
        r"|nouvo|sirius|winner|exciter|vespa|janus|motorbike|scooter|moto|xe\s?máy)\b",
        re.IGNORECASE,
    ),
    Category.APARTMENT: re.compile(
        r"\b(?:квартир\w*|апарт\w*|студи\w*|apartment|apartments|studio|condo"
        r"|căn\s?hộ|chung\s?cư)\b",
        re.IGNORECASE,
    ),
    Category.ROOM: re.compile(r"\b(?:комнат\w*|room|phòng)\b", re.IGNORECASE),
    Category.HOUSE: re.compile(r"\b(?:дом|дома|вилл\w*|house|villa|nhà)\b", re.IGNORECASE),
    Category.CAR: re.compile(r"\b(?:машин\w*|авто|car|ô\s?tô)\b", re.IGNORECASE),
    Category.BICYCLE: re.compile(r"\b(?:велосипед|bicycle|bike|xe\s?đạp)\b", re.IGNORECASE),
}


@dataclass(slots=True)
class GateResult:
    passed: bool
    reason: str
    has_price: bool = False
    is_offer: bool = False
    is_demand: bool = False
    categories: list[Category] = field(default_factory=list)

    def as_signals(self) -> dict[str, object]:
        """Кладётся в raw_messages.gate_signals — ступень 2 их переиспользует."""
        return {
            "has_price": self.has_price,
            "is_offer": self.is_offer,
            "is_demand": self.is_demand,
            "categories": [c.value for c in self.categories],
            "reason": self.reason,
        }


MIN_LENGTH = 25
MAX_LENGTH = 4000


def gate(text: str) -> GateResult:
    """Пропустить сообщение дальше по воронке или отбросить бесплатно."""
    stripped = text.strip()
    if len(stripped) < MIN_LENGTH:
        return GateResult(False, "too_short")
    if len(stripped) > MAX_LENGTH:
        # Простыни на 4000+ символов — это дайджесты и правила чата,
        # а не объявления.
        return GateResult(False, "too_long")

    has_price = bool(PRICE_RE.search(stripped))
    offer_match = OFFER_RE.search(stripped)
    demand_match = DEMAND_RE.search(stripped)
    is_offer = offer_match is not None
    is_demand = demand_match is not None
    categories = [cat for cat, pattern in CATEGORY_HINTS.items() if pattern.search(stripped)]

    result = GateResult(
        passed=False,
        reason="",
        has_price=has_price,
        is_offer=is_offer,
        is_demand=is_demand,
        categories=categories,
    )

    # Спрос — это чужой запрос, а не оффер. Отличить его мало по наличию
    # маркера: «в аренду» есть и в «ищу байк в аренду», и в «сдам в аренду».
    # Различает их только порядок — побеждает маркер, встретившийся первым.
    if is_demand and (
        offer_match is None or demand_match.start() < offer_match.start()  # type: ignore[union-attr]
    ):
        result.reason = "demand_not_offer"
        return result

    # «Honda Vision 2022, 350$» — глагола сделки нет, но это объявление.
    # Поэтому цена ИЛИ глагол, а не оба сразу.
    if not (has_price or is_offer):
        result.reason = "no_price_no_offer_verb"
        return result

    if not categories:
        result.reason = "no_category_hint"
        return result

    result.passed = True
    result.reason = "ok"
    return result
