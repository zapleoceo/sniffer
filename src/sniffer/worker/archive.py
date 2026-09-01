"""Оркестрация архива: сырьё → гейт → карточка.

Транзакция охватывает создание карточки и смену стадии, поэтому рестарт не
оставляет сообщение помеченным обработанным без ``listings``.
"""

from __future__ import annotations

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.fingerprint import fingerprint
from sniffer.pipeline.archive import (
    STAGE_DUPLICATE,
    STAGE_EXTRACTED,
    STAGE_REJECTED,
    classify,
    listing_from,
)

log = structlog.get_logger(__name__)

BATCH_SIZE = 50


class ArchivePipeline:
    async def tick(self) -> int:
        async with session_scope() as session:
            raw_repo = RawMessageRepository(session)
            messages = await raw_repo.list_by_stage(limit=BATCH_SIZE)
            handled = 0
            for raw in messages:
                if raw.id is None:  # pragma: no cover -- репозиторий всегда возвращает id
                    continue
                result = classify(raw)
                if not result.passed:
                    await raw_repo.set_stage(
                        [raw.id], STAGE_REJECTED, gate_signals=result.as_signals()
                    )
                    handled += 1
                    continue
                chat = await ChatRepository(session).get_by_tg_id(raw.chat_tg_id)
                if chat is None:
                    log.warning(
                        "pipeline.unknown_chat", chat_tg_id=raw.chat_tg_id, raw_message_id=raw.id
                    )
                    await raw_repo.set_stage(
                        [raw.id],
                        STAGE_REJECTED,
                        gate_signals={**result.as_signals(), "reason": "unknown_chat"},
                    )
                    handled += 1
                    continue
                # Отпечаток считаем от текста, а не берём сохранённый: строки
                # от старого коллектора несут побайтовый хеш, и кросспост по
                # ним не сходится. Заодно освежаем его в базе.
                digest = fingerprint(raw.text)
                if await raw_repo.has_listing_for(digest, besides=raw.id):
                    # Кросспост: то же объявление уже стало карточкой из другой
                    # группы. Вторую не заводим — клиенту она пришла бы дублем.
                    await raw_repo.set_stage(
                        [raw.id],
                        STAGE_DUPLICATE,
                        gate_signals=result.as_signals(),
                        text_hash=digest,
                    )
                    handled += 1
                    continue
                await ListingRepository(session).add(listing_from(raw, chat, result))
                await raw_repo.set_stage(
                    [raw.id], STAGE_EXTRACTED, gate_signals=result.as_signals(), text_hash=digest
                )
                handled += 1
            await session.commit()
            return handled
