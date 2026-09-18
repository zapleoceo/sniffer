"""Проверка живости каталога: перечитать известные объявления чата.

Догон истории (`ingest.py`) читает только новое сверху ленты, и карточка,
однажды созданная, дальше жила бы вечно: продавец удалил пост или исправил его
на «ПРОДАНО», а бот продолжал бы отвечать им клиентам. Здесь те же сообщения
перечитываются по номерам — это чтение, а не действие (CLAUDE.md, «Работа с
Telegram»): никому не видно и под `PEER_FLOOD` не подпадает.

Удалённое (Telegram отдаёт `None`) и отредактированное в «продано/сдано»
(`domain.listing_state.announces_closed`) гасится. За проход — один чат по кругу:
пятьдесят чатов при проходе раз в пятнадцать минут дают полный круг за полсуток,
и нагрузка на аккаунт — один-два запроса чтения за проход.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog

from sniffer.domain.listing_state import LISTING_MAX_AGE_DAYS, announces_closed
from sniffer.domain.records import Chat
from sniffer.sources.telegram_discover_reference import MessageLike

log = structlog.get_logger(__name__)

# Столько номеров Telegram отдаёт одним `get_messages(ids=...)`.
IDS_PER_CALL = 100
# Потолок карточек одного чата за проход: самый плотный чат (Arenda_Nyachangg,
# ~300 карточек в сутки) иначе съел бы проход целиком.
REFS_PER_CHAT = 300


class LivenessReader(Protocol):
    async def messages_by_ids(
        self, entity: int | str, ids: Sequence[int]
    ) -> Sequence[MessageLike | None]: ...


class LivenessStore(Protocol):
    async def active_chats(self, *, limit: int) -> list[Chat]: ...

    async def live_refs(self, chat: Chat, *, since: datetime) -> list[tuple[int, int]]: ...

    async def retire(self, listing_ids: list[int]) -> int: ...


@dataclass(slots=True)
class LivenessChecker:
    reader: LivenessReader
    store: LivenessStore
    chats_limit: int = 50
    # Номер чата в круге переживает проходы: коллектор держит объект между ними.
    position: int = field(default=0)

    async def run(self) -> int:
        chats = await self.store.active_chats(limit=self.chats_limit)
        if not chats:
            return 0
        chat = chats[self.position % len(chats)]
        self.position += 1
        try:
            return await self._check(chat)
        except Exception as exc:
            # Недоступный чат не останавливает круг: следующий проход возьмёт
            # следующий чат, а этот вернётся через круг.
            log.warning(
                "collector.liveness_failed", chat=chat.tg_id, error=f"{type(exc).__name__}: {exc}"
            )
            return 0

    async def _check(self, chat: Chat) -> int:
        since = datetime.now(UTC) - timedelta(days=LISTING_MAX_AGE_DAYS)
        refs = await self.store.live_refs(chat, since=since)
        if not refs:
            return 0
        dead: list[int] = []
        for start in range(0, len(refs), IDS_PER_CALL):
            batch = refs[start : start + IDS_PER_CALL]
            messages = await self._read(chat, [msg_id for _, msg_id in batch])
            for (listing_id, _), message in zip(batch, messages, strict=False):
                if message is None or announces_closed(str(message.message or "")):
                    dead.append(listing_id)
        retired = await self.store.retire(dead) if dead else 0
        log.info("collector.liveness_checked", chat=chat.tg_id, checked=len(refs), retired=retired)
        return retired

    async def _read(self, chat: Chat, ids: list[int]) -> Sequence[MessageLike | None]:
        """Сначала по tg_id, и лишь если он не разрешился — по имени.

        Порядок обратный догону истории намеренно. Догон в этом же проходе уже
        прочитал все чаты, их сущности лежат в кэше клиента, и числовой id
        разрешается без сети. Имя же стоит запроса `ResolveUsername`, а у него
        свой флуд-лимит: замер 18.09.2026 — паузы по 8 секунд, в которые
        проверка по имени добавляла свою долю.
        """
        try:
            return await self.reader.messages_by_ids(chat.tg_id, ids)
        except Exception as exc:
            if not chat.username:
                raise
            log.info("collector.liveness_by_username", chat=chat.tg_id, error=type(exc).__name__)
        return await self.reader.messages_by_ids(chat.username, ids)
