"""Проверка живости каталога: перечитать известные объявления чата.

Догон истории (`ingest.py`) читает только новое сверху ленты, и карточка,
однажды созданная, дальше жила бы вечно: продавец удалил пост или исправил его
на «ПРОДАНО», а бот продолжал бы отвечать им клиентам. Здесь те же сообщения
перечитываются по номерам — это чтение, а не действие (CLAUDE.md, «Работа с
Telegram»): никому не видно и под `PEER_FLOOD` не подпадает.

Удалённое (Telegram отдаёт `None`) и отредактированное в «продано/сдано»
(`domain.listing_state.announces_closed`) гасится. Исключение одно: если `None`
вернулся на ВСЕ проверенные карточки чата (от `ALL_GONE_THRESHOLD`), это похоже
на сбой или потерю доступа, и не гасится ничего — только предупреждение в лог.
За проход — один чат по кругу:
пятьдесят чатов при проходе раз в пятнадцать минут дают полный круг за полсуток,
и нагрузка на аккаунт — один-два запроса чтения за проход.

Внутри чата тоже круг: курсор `chats.liveness_listing_id` — докуда по возрастанию
`listing.id` дочитано. Каждый проход берёт следующую пачку активных карточек после
курсора, дошёл до конца — со следующего прохода с начала. Раньше читались 300
НОВЕЙШИХ, и 80% активных каталога (18 448 из 22 936) не перечитывались никогда.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog

from sniffer.domain.listing_state import (
    LISTING_MAX_AGE_DAYS,
    REASON_LIVENESS_CLOSED,
    REASON_LIVENESS_DELETED,
    announces_closed,
)
from sniffer.domain.records import Chat
from sniffer.sources.telegram_discover_reference import MessageLike

log = structlog.get_logger(__name__)

# Столько номеров Telegram отдаёт одним `get_messages(ids=...)`.
IDS_PER_CALL = 100
# Пачка карточек одного чата за проход: самый плотный чат (Arenda_Nyachangg,
# ~300 карточек в сутки) иначе съел бы проход целиком. Это размер шага курсора.
REFS_PER_CHAT = 300
# С такого числа проверенных карточек ответ «удалены все до одной» считается
# сбоем, а не удалением. Меньше — выборка слишком мала: чат с тремя карточками
# честно может потерять все три.
ALL_GONE_THRESHOLD = 5


class LivenessReader(Protocol):
    async def messages_by_ids(
        self, entity: int | str, ids: Sequence[int]
    ) -> Sequence[MessageLike | None]: ...


class LivenessStore(Protocol):
    async def active_chats(self, *, limit: int) -> list[Chat]: ...

    async def cursor(self, chat: Chat) -> int: ...

    async def save_cursor(self, chat: Chat, listing_id: int) -> None: ...

    async def live_refs(
        self, chat: Chat, *, since: datetime, after_id: int, limit: int
    ) -> list[tuple[int, int]]: ...

    async def retire(self, listing_ids: list[int], *, reason: str) -> int: ...


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
        stored = await self.store.cursor(chat)
        refs = await self.store.live_refs(chat, since=since, after_id=stored, limit=REFS_PER_CHAT)
        if not refs and stored:
            # Курсор стоял на последней карточке: круг замкнулся, не тратим проход.
            refs = await self.store.live_refs(chat, since=since, after_id=0, limit=REFS_PER_CHAT)
        if not refs:
            if stored:
                await self.store.save_cursor(chat, 0)
            return 0
        # Короткая пачка — конец круга: следующий проход начнёт с начала.
        next_cursor = max(listing_id for listing_id, _ in refs) if len(refs) >= REFS_PER_CHAT else 0
        pairs: list[tuple[int, MessageLike | None]] = []
        for start in range(0, len(refs), IDS_PER_CALL):
            batch = refs[start : start + IDS_PER_CALL]
            messages = await self._read(chat, [msg_id for _, msg_id in batch])
            pairs.extend(
                (listing_id, message)
                for (listing_id, _), message in zip(batch, messages, strict=False)
            )
        if len(pairs) >= ALL_GONE_THRESHOLD and all(message is None for _, message in pairs):
            # Все номера разом «удалены» — это не массовая уборка в чате, а
            # сбой чтения или потеря доступа (кикнули, сессия, смена чата): Telegram
            # отдаёт `None` и за закрытый доступ. Снять сейчас значит погасить
            # живой чат целиком; следующий круг прочитает его заново, а настоящие
            # удаления подберёт частичный ответ или возраст карточки.
            log.warning("collector.liveness_all_gone", chat=chat.tg_id, checked=len(pairs))
            # Курсор идёт вперёд и здесь: иначе честно вычищенная пачка (чат убрал
            # старые посты разом) держала бы круг на месте, и дальше него никто не
            # читался бы. Эту пачку круг перечитает на следующем обороте.
            await self.store.save_cursor(chat, next_cursor)
            return 0
        deleted = [listing_id for listing_id, message in pairs if message is None]
        closed = [
            listing_id
            for listing_id, message in pairs
            if message is not None and announces_closed(str(message.message or ""))
        ]
        retired = 0
        for ids, reason in ((deleted, REASON_LIVENESS_DELETED), (closed, REASON_LIVENESS_CLOSED)):
            if ids:
                retired += await self.store.retire(ids, reason=reason)
        # Курсор сдвигается после снятия: упавшая проверка (исключение выше) пачку
        # не теряет, её возьмёт следующий оборот.
        await self.store.save_cursor(chat, next_cursor)
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
