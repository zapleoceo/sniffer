"""Детерминированная первая обработка архива Telegram.

LLM не является обязательным для появления карточки: если сообщение уже прошло
бесплатный гейт, из него можно безопасно сделать минимальную карточку со
ссылкой, категорией и только явно указанной ценой. Более глубокое извлечение
позже улучшает эту карточку, но не блокирует поиск и подписки.
"""

from __future__ import annotations

from decimal import Decimal

from sniffer.domain.prices import price_hint
from sniffer.domain.records import Chat, Listing, RawMessage
from sniffer.pipeline.gate import CategoryDetector, GateResult, gate

STAGE_GATED = "gated"
STAGE_EXTRACTED = "extracted"
STAGE_REJECTED = "rejected"
# Кросспост: объявление уже стало карточкой из другой группы. Отдельная стадия,
# а не `rejected`: сообщение не мусор, просто карточка у него уже есть, и
# разбираться в отклонённых потом придётся именно по этой разнице.
STAGE_DUPLICATE = "duplicate"


def classify(raw: RawMessage, *, category_hints: CategoryDetector | None = None) -> GateResult:
    """Один гейт на архив и на будущую обработку задач.

    Детектор категорий внедряется процессом (воркер даёт словарь поиска), а не
    берётся здесь: воронка про поиск не знает — это обратная зависимость слоёв.
    """
    return gate(raw.text, category_hints=category_hints)


def listing_from(
    raw: RawMessage,
    chat: Chat,
    result: GateResult,
    *,
    deal_type: str = "sell",
    attributes: dict[str, object] | None = None,
    city: str = "",
) -> Listing:
    """Минимальная честная карточка из прошедшего гейт сообщения.

    `city` — город, названный в САМОМ объявлении; пусто — берём город чата.
    Разница не косметическая, и видна она не на всех чатах. Пока в реестре были
    только нячангские группы, город чата и город лота совпадали почти всегда.
    Первая же общевьетнамская барахолка (`@vietavito`, «все барахолки») это
    ломает: внутри объявления со всей страны, и город чата приписал бы ханойскую
    квартиру Нячангу — а матчер фильтрует ровно по этому полю
    (`matching/rules.py`), то есть ложь дошла бы до выдачи как правда.

    Порядок «текст лота главнее чата» тот же, что у разбора запроса клиента:
    `parse_query` кладёт `city or default_city`. Заодно чинится случай, который
    был и раньше: продавец из нячангской группы, продающий байк в Дананге.
    """
    if raw.id is None:
        raise ValueError("raw message without database id")
    if not result.passed or not result.categories:
        raise ValueError("cannot build listing from rejected message")
    price_raw, price_vnd = price_hint(raw.text)
    return Listing(
        raw_message_id=raw.id,
        source="telegram_archive",
        external_id=f"{raw.chat_tg_id}:{raw.msg_id}",
        deal_type=deal_type,
        category=result.categories[0].value,
        city=city or chat.city,
        title=_title(raw.text),
        summary=_summary(raw.text),
        tg_link=_link(chat.tg_id, chat.username, raw.msg_id),
        posted_at=raw.posted_at,
        seller_id=raw.seller_id,
        price_amount=Decimal(price_vnd) if price_vnd is not None else None,
        price_currency="VND" if price_vnd is not None else None,
        price_period="once" if price_vnd is not None else None,
        attributes=dict(attributes or {}),
        confidence=0.55 if price_raw else 0.4,
        lang=None,
    )


def _link(tg_id: int, username: str | None, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}/{msg_id}"
    digits = str(abs(tg_id))
    internal = digits[3:] if tg_id < 0 and digits.startswith("100") else digits
    return f"https://t.me/c/{internal}/{msg_id}"


def _title(text: str) -> str:
    line = next((line.strip() for line in text.splitlines() if line.strip()), "объявление")
    return line[:180]


def _summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:800]
