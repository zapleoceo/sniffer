"""Детерминированная первая обработка архива Telegram.

LLM не является обязательным для появления карточки: если сообщение уже прошло
бесплатный гейт, из него можно безопасно сделать минимальную карточку со
ссылкой, категорией и только явно указанной ценой. Более глубокое извлечение
позже улучшает эту карточку, но не блокирует поиск и подписки.
"""

from __future__ import annotations

from sniffer.domain.passport import Category, Intent, default_deal_type
from sniffer.domain.prices import parse_price
from sniffer.domain.records import Chat, Listing, RawMessage
from sniffer.pipeline.gate import CategoryDetector, GateResult, gate
from sniffer.pipeline.listing_facts import fact_columns
from sniffer.pipeline.listing_price import PriceColumns, price_columns

STAGE_GATED = "gated"
STAGE_EXTRACTED = "extracted"
STAGE_REJECTED = "rejected"
# Кросспост: объявление уже стало карточкой из другой группы. Отдельная стадия,
# а не `rejected`: сообщение не мусор, просто карточка у него уже есть, и
# разбираться в отклонённых потом придётся именно по этой разнице.
STAGE_DUPLICATE = "duplicate"


def offer_deal_type(intent: Intent | None, category: Category | None = None) -> str:
    """Сторона объявления: глагол сделки, если он есть, иначе умолчание категории.

    Глагол аренды в тексте ОБЪЯВЛЕНИЯ, прошедшего гейт предложения, означает,
    что владелец сдаёт: клиентское «аренда» и «сдам» здесь одно и то же. Без
    этого голые заголовки вроде «АРЕНДА БАЙКОВ» становились карточками продажи.

    Без глагола решает категория (`domain.passport.default_deal_type`): жильё в
    Нячанге сдают, технику продают. Пока умолчанием для всего была продажа, 2707
    из 3714 карточек жилья за 28 дней (замер 12.09.2026) числились продажей при
    тексте про аренду, и арендатор по стороне сделки не находил ничего.
    """
    if intent in {Intent.RENT, Intent.RENT_OUT}:
        return Intent.RENT_OUT.value
    if intent is Intent.SELL:
        return Intent.SELL.value
    return default_deal_type(category)


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

    Город самого поста (`facts.city`) главнее и города чата, и умолчания разбора запроса:
    `parse_query` отдаёт город чата, когда текст города не называет, и без этого лот из
    Дананга в нячангской группе оставался нячангским (решение владельца 04.10.2026).
    Читается город только у Нячанга и Дананга — у остальных справочника мест нет.

    Район, язык, заголовок и факты (площадь, этаж, удобства, год, пробег…) читает
    `listing_facts.fact_columns`; явные `attributes` разбора запроса главнее прочитанного.
    Заголовок — первая содержательная строка поста, а не первая непустая: так «AN-HOME» и
    «#нячанг #аренда» перестали быть названием 1900 карточек.
    """
    if raw.id is None:
        raise ValueError("raw message without database id")
    if not result.passed or not result.categories:
        raise ValueError("cannot build listing from rejected message")
    category = result.categories[0].value
    prices = price_columns(parse_price(raw.text, category=category, deal_type=deal_type), deal_type)
    facts = fact_columns(
        raw.text,
        category=category,
        deal_type=deal_type,
        attributes=attributes,
        city=city or chat.city,
        monthly_rent=_monthly_rent_vnd(prices),
    )
    return Listing(
        raw_message_id=raw.id,
        source="telegram_archive",
        external_id=f"{raw.chat_tg_id}:{raw.msg_id}",
        deal_type=deal_type,
        category=category,
        city=facts.city or city or chat.city,
        district=facts.district,
        title=facts.title,
        summary=_summary(raw.text),
        tg_link=_link(chat.tg_id, chat.username, raw.msg_id),
        posted_at=raw.posted_at,
        seller_id=raw.seller_id,
        price_amount=prices.amount,
        price_currency=prices.currency,
        price_period=prices.period,
        attributes={**facts.attributes, **prices.attributes},
        confidence=0.55 if prices.amount is not None or prices.attributes else 0.4,
        lang=facts.lang,
    )


def _monthly_rent_vnd(prices: PriceColumns) -> int | None:
    """Месячная аренда в донгах, если цена карточки — именно она."""
    if prices.amount is None or prices.currency != "VND" or prices.period != "month":
        return None
    return int(prices.amount)


def _link(tg_id: int, username: str | None, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}/{msg_id}"
    digits = str(abs(tg_id))
    internal = digits[3:] if tg_id < 0 and digits.startswith("100") else digits
    return f"https://t.me/c/{internal}/{msg_id}"


def _summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:800]
