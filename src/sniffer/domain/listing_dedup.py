"""План уборки каталога: какие карточки слить и какие ссылки переписать.

Чистая функция над уже прочитанными строками — без базы, поэтому план можно
показать (`--dry-run`) ровно тем же кодом, которым он потом исполняется.

Правила слияния, решение владельца от 16.11.2026:

- **остаётся первоисточник** — карточка самого раннего поста группы (ссылка,
  текст, `raw_message_id` не трогаются);
- **цена — самая свежая**: из самой поздней карточки группы, у которой цена
  есть; вместе с ней сдвигается `posted_at`, иначе слитая карточка погасла бы
  раньше живого перепоста;
- **ничего не удаляется**: дубли гасятся (`is_active=false`, причина
  `duplicate_merged`), снимок «как было» уходит в журнал уборки.

Группа — одно объявление в одном городе, категории и стороне сделки: тот же
текст с другим направлением сделки — исправление, а не дубль.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sniffer.domain.origin import Origin, PostRef, canonical_telegram_url

# Цена, которая не легла в колонки (`pipeline/listing_price.py`), живёт в атрибутах
# и переезжает вместе с ценой — и на входе (`pipeline/duplicates.py`), и при уборке.
PRICE_ATTRIBUTES = frozenset(
    {"price_up_to", "rate_amount", "rate_currency", "rate_per", "rate_up_to"}
)


@dataclass(frozen=True, slots=True)
class CardKeys:
    """Карточка в объёме, нужном плану. Текст сюда не входит — только его ключи."""

    id: int
    posted_at: datetime
    city: str
    category: str
    deal_type: str
    tg_link: str
    exact: str
    near: str | None = None
    origin: Origin | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    price_period: str | None = None
    price_usd_month: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Merge:
    canonical_id: int
    duplicate_ids: tuple[int, ...]
    # Карточка, чья цена становится ценой первоисточника; `None` — цена не меняется.
    price_from_id: int | None
    posted_at: datetime


ACTION_CANONICAL_FORM = "canonical_form"  # та же публикация, другая запись ссылки
ACTION_POINTER_TARGET = "pointer_target"  # карточка-указатель → ссылка на оригинал
ACTION_MERGE_INTO_TARGET = "merge_into_target"  # у оригинала уже есть своя карточка
ACTION_NOT_PRIMARY = "not_primary"  # первоисточник не определён — карточка снимается


@dataclass(frozen=True, slots=True)
class LinkFix:
    listing_id: int
    action: str
    before: str
    after: str | None = None
    target_listing_id: int | None = None


# Ссылка на пост → (каноническая ссылка, id активной карточки оригинала, если есть).
Resolve = Callable[[PostRef], tuple[str | None, int | None]]


def plan_links(cards: Iterable[CardKeys], resolve: Resolve) -> list[LinkFix]:
    fixes: list[LinkFix] = []
    for card in cards:
        origin = card.origin
        if origin is not None and origin.kind is not None and not origin.primary:
            fixes.append(_pointer_fix(card, origin, resolve))
            continue
        canonical = canonical_telegram_url(card.tg_link)
        if canonical is not None and canonical != card.tg_link:
            fixes.append(LinkFix(card.id, ACTION_CANONICAL_FORM, card.tg_link, canonical))
    return fixes


def _pointer_fix(card: CardKeys, origin: Origin, resolve: Resolve) -> LinkFix:
    if origin.post is None:
        return LinkFix(card.id, ACTION_NOT_PRIMARY, card.tg_link)
    url, target = resolve(origin.post)
    if target is not None and target != card.id:
        return LinkFix(card.id, ACTION_MERGE_INTO_TARGET, card.tg_link, url, target)
    if url is None:
        # Ссылка вида t.me/c/…: оригинал есть, но клиенту он не откроется.
        return LinkFix(card.id, ACTION_NOT_PRIMARY, card.tg_link)
    return LinkFix(card.id, ACTION_POINTER_TARGET, card.tg_link, url)


@dataclass(slots=True)
class _Groups:
    parent: dict[int, int] = field(default_factory=dict)

    def find(self, item: int) -> int:
        root = self.parent.setdefault(item, item)
        while root != self.parent[root]:
            root = self.parent[root]
        self.parent[item] = root
        return root

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def plan_merges(cards: Sequence[CardKeys], *, near: bool) -> list[Merge]:
    """Группы дублей → слияния. `near` включает ключ без цены (перепост с новой ценой)."""
    groups = _Groups()
    first_by_key: dict[tuple[str, str, str, str], int] = {}
    for card in cards:
        groups.find(card.id)
        keys = [f"text:{card.exact}"]
        if near and card.near is not None:
            keys.append(f"near:{card.near}")
        for key in keys:
            scoped = (card.city, card.category, card.deal_type, key)
            if scoped in first_by_key:
                groups.union(first_by_key[scoped], card.id)
            else:
                first_by_key[scoped] = card.id
    members: dict[int, list[CardKeys]] = {}
    for card in cards:
        members.setdefault(groups.find(card.id), []).append(card)
    merges = [_merge(group) for group in members.values() if len(group) > 1]
    return sorted(merges, key=lambda merge: merge.canonical_id)


def _merge(group: list[CardKeys]) -> Merge:
    # Первоисточник — самый ранний пост, который клиент может открыть: ссылка
    # `t.me/c/…` открывается только участникам чата (сухой прогон 16.11.2026 нашёл
    # такую «самую раннюю» карточку в группе с публичными копиями).
    canonical = min(group, key=lambda card: (not _public(card), card.posted_at, card.id))
    newest = max(group, key=lambda card: (card.posted_at, card.id))
    priced = [card for card in group if card.price_amount is not None]
    donor = max(priced, key=lambda card: (card.posted_at, card.id)) if priced else None
    price_from = None
    if donor is not None and donor.id != canonical.id and _price(donor) != _price(canonical):
        price_from = donor.id
    return Merge(
        canonical_id=canonical.id,
        duplicate_ids=tuple(sorted(card.id for card in group if card.id != canonical.id)),
        price_from_id=price_from,
        posted_at=newest.posted_at,
    )


def _public(card: CardKeys) -> bool:
    return not card.tg_link.startswith("https://t.me/c/")


def _price(card: CardKeys) -> tuple[object, ...]:
    # `price_usd_month` — производная, её расхождение при той же цене сменой цены не считаем.
    return (card.price_amount, card.price_currency, card.price_period)
