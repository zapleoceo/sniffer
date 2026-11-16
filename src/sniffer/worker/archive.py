"""Оркестрация архива: сырьё → гейт → карточка.

Транзакция охватывает создание карточки и смену стадии, поэтому рестарт не
оставляет сообщение помеченным обработанным без `listings`.

Одно сообщение обрабатывается независимо от остальных, и это не запас
прочности, а урок. Живой отказ 01.09.2026: объявление с ценой «21.500.000 млн
VND» дало 21.5 триллиона донгов, вставка упала на `NUMERIC(14,2)`, и воркер
ушёл в ЦИКЛ ПЕРЕЗАПУСКА — то есть одно кривое объявление навсегда остановило
обработку всех остальных. Пачка, падающая целиком из-за одной строки, — это не
надёжность, это единая точка отказа с чужим текстом на входе.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.fingerprint import fingerprint
from sniffer.domain.listing_state import REASON_SUPERSEDED, is_stale
from sniffer.domain.records import RawMessage
from sniffer.pipeline.archive import (
    STAGE_DUPLICATE,
    STAGE_EXTRACTED,
    STAGE_REJECTED,
    STAGE_STALE,
    classify,
    is_primary,
    listing_from,
    offer_deal_type,
)
from sniffer.pipeline.duplicates import merge_values
from sniffer.search import vocabulary
from sniffer.search.intake_rules import parse_query

log = structlog.get_logger(__name__)

BATCH_SIZE = 50


async def _with_origin_name(raw: RawMessage, chats: ChatRepository) -> RawMessage:
    """Имя канала-оригинала из реестра, если в ответе Telegram его не было.

    Без имени ссылка на оригинал невозможна, и пересылка считалась бы «без
    адреса». Канал из нашего реестра мы знаем и так.
    """
    if raw.origin_username or raw.origin_chat_tg_id is None:
        return raw
    origin = await chats.get_by_tg_id(raw.origin_chat_tg_id)
    if origin is None or not origin.username:
        return raw
    return replace(raw, origin_username=origin.username)


Now = Callable[[], datetime]


class ArchivePipeline:
    def __init__(self, *, now: Now = lambda: datetime.now(UTC), near: bool = False) -> None:
        self._now = now
        self._near = near

    async def tick(self) -> int:
        async with session_scope() as session:
            repo = RawMessageRepository(session)
            handled = 0
            for _ in range(BATCH_SIZE):
                raw = await repo.take_by_stage()
                if raw is None:
                    break
                handled += await self._safely(raw, session)
            return handled

    async def _safely(self, raw: RawMessage, session: AsyncSession) -> int:
        """Одно сообщение, своей транзакцией. Падение не уносит соседей.

        Широкий `except` намеренно: перечислять причины, по которым чужой текст
        не разобрался, значит однажды встать на неназванной. Решение одно и то
        же для любой — пометить сообщение отклонённым с причиной в
        `gate_signals` и идти дальше; разбираться с ним потом по логу.
        """
        try:
            moved = await self._one(raw, session)
            await session.commit()
            return moved
        except Exception as exc:
            await session.rollback()
            log.warning(
                "pipeline.message_failed",
                raw_message_id=raw.id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await RawMessageRepository(session).set_stage(
                [raw.id or 0], STAGE_REJECTED, gate_signals={"reason": "pipeline_error"}
            )
            await session.commit()
            return 1

    async def _one(self, raw: RawMessage, session: AsyncSession) -> int:
        repo = RawMessageRepository(session)
        assert raw.id is not None
        if is_stale(raw.posted_at, self._now()):
            # Старше срока актуальности: ни карточки, ни `refresh` (он перезаписал
            # бы `posted_at` и оживил погашенную), ни обращения к модели. Догон
            # истории чата приносит такое тысячами. Сырьё остаётся (90 дней).
            await repo.set_stage([raw.id], STAGE_STALE, gate_signals={"reason": "stale"})
            return 1
        # Знание «какими словами/марками/моделями зовётся категория» внедряем из
        # поиска: воркер — процесс, композирующий слои, и он же берёт отсюда
        # `parse_query`. Это не ребро pipeline→search, а его отсутствие.
        result = classify(raw, category_hints=vocabulary.category_hints)
        if not result.passed:
            await repo.set_stage([raw.id], STAGE_REJECTED, gate_signals=result.as_reject_signals())
            return 1

        chats = ChatRepository(session)
        raw = await _with_origin_name(raw, chats)
        assert raw.id is not None
        if not is_primary(raw):
            # Пересылка без адреса оригинала или сообщение-ссылка: карточка с такой
            # ссылкой вела бы на копию, а не на объявление (docs/primary-sources.md).
            await repo.set_stage(
                [raw.id],
                STAGE_REJECTED,
                gate_signals={"reason": "not_primary", "origin_kind": raw.origin_kind},
            )
            return 1
        chat = await chats.get_by_tg_id(raw.chat_tg_id)
        if chat is None:
            log.warning("pipeline.unknown_chat", chat_tg_id=raw.chat_tg_id, raw_message_id=raw.id)
            await repo.set_stage(
                [raw.id],
                STAGE_REJECTED,
                gate_signals={"reason": "unknown_chat"},
            )
            return 1

        # Отпечаток считаем от текста, а не берём сохранённый: строки от старого
        # коллектора несут побайтовый хеш, и кросспост по ним не сходится.
        # Заодно освежаем его в базе.
        digest = fingerprint(raw.text)
        await repo.lock_fingerprint(digest)
        parsed = parse_query(raw.text, default_city=chat.city)
        candidate = listing_from(
            raw,
            chat,
            result,
            # Глагол сделки из текста, иначе умолчание категории: жильё
            # сдают, технику продают (`pipeline.archive.offer_deal_type`).
            deal_type=offer_deal_type(parsed.intent, result.categories[0]),
            attributes=dict(parsed.attributes),
            # Город из текста лота. `parse_query` уже получил его выше с
            # городом чата по умолчанию — оставалось только донести до
            # карточки, а она брала город чата напрямую.
            city=parsed.city or "",
        )
        listings = ListingRepository(session)
        existing = await listings.get_by_fingerprint(digest, besides=raw.id)
        if self._near and existing is None and raw.identity_hash is not None:
            # Тот же текст с другой ценой: перепост, а не новое объявление.
            await repo.lock_fingerprint(raw.identity_hash)
            existing = await listings.get_by_identity(
                raw.identity_hash,
                besides=raw.id,
                city=candidate.city,
                category=candidate.category,
                deal_type=candidate.deal_type,
            )
        stage = STAGE_EXTRACTED
        if existing is not None:
            assert existing.id is not None
            identity_changed = (
                existing.deal_type,
                existing.category,
                existing.city,
            ) != (candidate.deal_type, candidate.category, candidate.city)
            if identity_changed:
                # A newly detected direction/category is a real correction, not
                # another notification-free repost.  Give it a new cursor id so
                # existing subscriptions can see the repaired offer.
                await listings.deactivate(existing.id, reason=REASON_SUPERSEDED)
                await listings.add(candidate)
            else:
                # Карточка остаётся на первоисточнике; от свежей копии — цена и свежесть.
                values = merge_values(existing, candidate)
                await listings.update_fields(existing.id, values)
                if values.get("raw_message_id") != raw.id:
                    stage = STAGE_DUPLICATE
        else:
            await listings.add(candidate)
        await repo.set_stage([raw.id], stage, gate_signals=result.as_signals(), text_hash=digest)
        return 1
