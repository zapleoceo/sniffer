"""Задача воркера: каждую карточку Telegram-архива читает модель.

Карточка появляется сразу после бесплатного гейта — поиск не ждёт модели
(`pipeline/archive.py`: «LLM не обязателен для появления карточки»). Следом эта
задача берёт непрочитанные пачкой, свежие первыми, и по вердикту
`verifier.offer_screen` гасит не-товар (обмен валют, услуги, распродажи
списком, спрос) и уточняет у товара категорию, сторону сделки и
характеристики — электро или ДВС, марку, объём, число комнат.

Накопленное до 18.09.2026 чистится той же задачей: у него `screened_at` пуст,
и оно встаёт в очередь за свежим. Отдельной разовой чистки нет — второй код
для той же работы разъехался бы с первым.

Отказ брокера — не авария. Дневной лимит проекта останавливает задачу до
полуночи UTC (ретрай бюджета не создаёт), иная ошибка — на десять минут.
Карточка без вердикта остаётся в каталоге как была: проверка улучшает
каталог, а не условие его работы.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sniffer.broker.client import BrokerCapError, BrokerClient, BrokerError
from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.passport import Category
from sniffer.domain.records import Listing
from sniffer.search.intake_rules import parse_query
from sniffer.search.motorbike_models import MOTORBIKE_BRANDS
from sniffer.verifier.offer_screen import (
    SCREEN_BATCH,
    OfferVerdict,
    StructuredBroker,
    screen_offers,
)

log = structlog.get_logger(__name__)

SOURCE = "telegram_archive"
# Пачек за проход: остальные шаги воркера (архив, сопоставление) не должны
# ждать, пока модель дочитает семь тысяч накопленных карточек.
BATCHES_PER_TICK = 2
RETRY_AFTER_S = 600.0
_HOUSING = {Category.APARTMENT.value, Category.ROOM.value, Category.HOUSE.value}


def screened_fields(listing: Listing, verdict: OfferVerdict) -> dict[str, Any]:
    """Вердикт → аргументы `ListingRepository.apply_screen`."""
    note = f"{verdict.kind}/{verdict.category}: {verdict.why}"
    if not verdict.is_offer:
        return {"keep": False, "note": note}
    if verdict.category == listing.category:
        attributes = dict(listing.attributes)
    else:
        # Предмет сменился — прежние свойства читались под другую категорию:
        # у квартиры не бывает коробки передач.
        text = f"{listing.title}\n{listing.summary}"
        attributes = dict(parse_query(text, default_city=listing.city).attributes)
    if verdict.category == Category.MOTORBIKE.value:
        if verdict.power != "unknown":
            attributes["power"] = verdict.power
        if verdict.brand in MOTORBIKE_BRANDS:
            attributes.setdefault("brand", verdict.brand)
        if verdict.engine_cc is not None:
            attributes.setdefault("engine_cc", verdict.engine_cc)
    else:
        attributes.pop("power", None)
    if verdict.category in _HOUSING and verdict.rooms is not None:
        attributes.setdefault("rooms", verdict.rooms)
    return {
        "keep": True,
        "note": note,
        "category": verdict.category,
        "deal_type": verdict.deal,
        "attributes": attributes,
    }


Unscreened = Callable[[int], Awaitable[list[Listing]]]
Apply = Callable[[int, dict[str, Any]], Awaitable[None]]
Clock = Callable[[], float]


async def _unscreened(limit: int) -> list[Listing]:
    async with session_scope() as session:
        return await ListingRepository(session).unscreened(SOURCE, limit=limit)


async def _apply(listing_id: int, fields: dict[str, Any]) -> None:
    async with session_scope() as session:
        await ListingRepository(session).apply_screen(listing_id, **fields)
        await session.commit()


def _seconds_to_utc_midnight() -> float:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return (midnight - now).total_seconds()


class Screening:
    """Пачки непрочитанных карточек → вердикт модели → каталог."""

    def __init__(
        self,
        *,
        broker: StructuredBroker | None = None,
        unscreened: Unscreened = _unscreened,
        apply: Apply = _apply,
        clock: Clock = time.monotonic,
    ) -> None:
        self._broker = broker
        self._unscreened, self._apply, self._clock = unscreened, apply, clock
        self._paused_until = 0.0

    def _client(self) -> StructuredBroker | None:
        if self._broker is None and get_settings().broker_project_key:
            from sniffer.broker.usage import default_usage_sink

            self._broker = BrokerClient(usage=default_usage_sink)
        return self._broker

    async def tick(self) -> int:
        """Сколько карточек прочитано за проход."""
        broker = self._client()
        if broker is None or self._clock() < self._paused_until:
            return 0
        done = 0
        for _ in range(BATCHES_PER_TICK):
            batch = await self._unscreened(SCREEN_BATCH)
            if not batch:
                break
            try:
                verdicts = await screen_offers(
                    [f"{row.title}\n{row.summary}" for row in batch], broker
                )
            except BrokerCapError:
                self._paused_until = self._clock() + _seconds_to_utc_midnight()
                log.warning("screening.cap_reached")
                break
            except (BrokerError, TimeoutError, OSError) as exc:
                self._paused_until = self._clock() + RETRY_AFTER_S
                log.warning("screening.broker_failed", error=f"{type(exc).__name__}: {exc}"[:200])
                break
            done += await self._record(batch, verdicts)
        return done

    async def _record(self, batch: list[Listing], verdicts: list[OfferVerdict | None]) -> int:
        dropped = 0
        for row, verdict in zip(batch, verdicts, strict=True):
            assert row.id is not None
            if verdict is None:
                # Модель номер пропустила. Помечаем прочитанным без изменений,
                # иначе та же пачка вставала бы в голову очереди вечно.
                await self._apply(row.id, {"keep": True, "note": "no_verdict"})
                continue
            fields = screened_fields(row, verdict)
            dropped += 0 if fields["keep"] else 1
            await self._apply(row.id, fields)
        log.info("screening.batch", read=len(batch), dropped=dropped)
        return len(batch)
