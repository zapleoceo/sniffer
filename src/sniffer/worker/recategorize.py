"""Пересчёт категории у карточек, созданных прежним правилом.

Правило категории поменялось 18.09.2026: предмет — тот, что назван в тексте
первым, а русские слова рынка узнаются с падежными окончаниями
(`search.vocabulary.category_hints`). Новые карточки воронка создаёт уже по
новому правилу, но накопленные остались бы с прежней, неверной категорией:
замер — 72 активные «мотобайковые» карточки были квартирами.

Знание о категории живёт в Python, а не в SQL, поэтому пересчёт делает воркер,
а не миграция: вторая копия правила в регулярном выражении SQL разъехалась бы с
первой. Проход один на старт процесса, пачками по id; идемпотентен — совпавшая
категория не трогается, повторный проход ничего не меняет. Вместе с категорией
пересчитываются сторона сделки и атрибуты: у квартиры не бывает коробки
передач, у байка — числа спален.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.records import Listing
from sniffer.pipeline.archive import offer_deal_type
from sniffer.search.intake_rules import parse_query
from sniffer.search.vocabulary import category_hints

log = structlog.get_logger(__name__)

SOURCE = "telegram_archive"
PAGE = 500


def corrected(listing: Listing) -> tuple[str, str, dict[str, object]] | None:
    """Новая категория, сторона и атрибуты — или `None`, если менять нечего."""
    text = f"{listing.title}\n{listing.summary}"
    named = category_hints(text)
    if not named or named[0].value == listing.category:
        return None
    category = named[0]
    parsed = parse_query(text, default_city=listing.city)
    return category.value, offer_deal_type(parsed.intent, category), dict(parsed.attributes)


Page = Callable[[int, int], Awaitable[list[Listing]]]
Apply = Callable[[int, str, str, dict[str, object]], Awaitable[None]]


async def _page(after_id: int, limit: int) -> list[Listing]:
    async with session_scope() as session:
        return await ListingRepository(session).active_page(SOURCE, after_id=after_id, limit=limit)


async def _apply(listing_id: int, category: str, deal_type: str, attrs: dict[str, object]) -> None:
    async with session_scope() as session:
        await ListingRepository(session).reclassify(
            listing_id, category=category, deal_type=deal_type, attributes=attrs
        )
        await session.commit()


class Recategorize:
    """Один проход по активным карточкам после старта процесса."""

    def __init__(self, *, page: Page = _page, apply: Apply = _apply, size: int = PAGE) -> None:
        self._page, self._apply, self._size = page, apply, size
        self._cursor = 0
        self._done = False
        self._changed = 0

    async def tick(self) -> int:
        if self._done:
            return 0
        rows = await self._page(self._cursor, self._size)
        if not rows:
            self._done = True
            log.info("listings.recategorized", changed=self._changed)
            return 0
        changed = 0
        for row in rows:
            assert row.id is not None
            self._cursor = row.id
            fix = corrected(row)
            if fix is not None:
                await self._apply(row.id, *fix)
                changed += 1
        self._changed += changed
        # Ненулевой возврат держит цикл воркера без паузы, пока страницы идут.
        return max(changed, 1)
