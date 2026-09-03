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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.fingerprint import fingerprint
from sniffer.domain.passport import Intent
from sniffer.domain.records import RawMessage
from sniffer.pipeline.archive import (
    STAGE_DUPLICATE,
    STAGE_EXTRACTED,
    STAGE_REJECTED,
    classify,
    listing_from,
)
from sniffer.search import vocabulary
from sniffer.search.intake_rules import parse_query

log = structlog.get_logger(__name__)

BATCH_SIZE = 50


class ArchivePipeline:
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
        # Знание «какими словами/марками/моделями зовётся категория» внедряем из
        # поиска: воркер — процесс, композирующий слои, и он же берёт отсюда
        # `parse_query`. Это не ребро pipeline→search, а его отсутствие.
        result = classify(raw, category_hints=vocabulary.category_hints)
        if not result.passed:
            await repo.set_stage([raw.id], STAGE_REJECTED, gate_signals=result.as_signals())
            return 1

        chat = await ChatRepository(session).get_by_tg_id(raw.chat_tg_id)
        if chat is None:
            log.warning("pipeline.unknown_chat", chat_tg_id=raw.chat_tg_id, raw_message_id=raw.id)
            await repo.set_stage(
                [raw.id],
                STAGE_REJECTED,
                gate_signals={**result.as_signals(), "reason": "unknown_chat"},
            )
            return 1

        # Отпечаток считаем от текста, а не берём сохранённый: строки от старого
        # коллектора несут побайтовый хеш, и кросспост по ним не сходится.
        # Заодно освежаем его в базе.
        digest = fingerprint(raw.text)
        await repo.lock_fingerprint(digest)
        if await repo.has_listing_for(digest, besides=raw.id):
            # Кросспост: то же объявление уже стало карточкой из другой группы.
            await repo.set_stage(
                [raw.id], STAGE_DUPLICATE, gate_signals=result.as_signals(), text_hash=digest
            )
            return 1

        parsed = parse_query(raw.text, default_city=chat.city)
        deal_type = (
            parsed.intent.value
            if parsed.intent in {Intent.SELL, Intent.RENT_OUT}
            else Intent.SELL.value
        )
        await ListingRepository(session).add(
            listing_from(
                raw,
                chat,
                result,
                deal_type=deal_type,
                attributes=dict(parsed.attributes),
                # Город из текста лота. `parse_query` уже получил его выше с
                # городом чата по умолчанию — оставалось только донести до
                # карточки, а она брала город чата напрямую.
                city=parsed.city or "",
            )
        )
        await repo.set_stage(
            [raw.id], STAGE_EXTRACTED, gate_signals=result.as_signals(), text_hash=digest
        )
        return 1
