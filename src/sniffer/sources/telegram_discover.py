"""Разведка чатов: найти новые группы и отобрать их, не вступая.

Продукт разведки — не карточки, а строки в очереди кандидатов, а после
вступления (`telegram_discover_joiner`) — в реестре `chats`. Читает их дальше
обычный адаптер `telegram_groups`, и ни одной его строки для этого менять не
пришлось: реестр — общая точка, разведка в него пишет, адаптер из него читает.
Поэтому `ChatDiscovery` и не наследует `Source`: у него нет `search()`, который
вернул бы `RawItem`, и притворяться источником выдачи было бы враньём в реестре
адаптеров.

Отбор устроен так, чтобы решение принималось **до** вступления, и это не
оптимизация, а единственная возможность: **выйти из чата нельзя**. Закрытый
список CLAUDE.md знает два действия — вступление и беззвучный режим, — и
`LeaveChannel` в него не входит. Ошибка вступления не откатывается ничем: она
навсегда занимает место под потолком числа чатов и стоит одного из трёх
суточных слотов.

Читать, не вступая, Telegram даёт двумя способами, и оба здесь используются:
`resolve_username` для публичной группы и `messages.checkChatInvite` для
закрытой. Оба отдают тип, название и описание. Поэтому у приглашения нет
поблажки: `screen()` один на оба пути, и кандидат по хэшу проходит те же
ворота, что кандидат по имени.

Кандидаты берутся из сообщений, которые и так проходят через воронку: отдельного
обхода чатов ради разведки нет — он стоил бы тех же лимитов, что и поиск.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import structlog

from sniffer.config import get_settings
from sniffer.sources.telegram_discover_links import candidates_from
from sniffer.sources.telegram_discover_reference import (
    MAX_SEARCH_RESULTS,
    REJECT_UNRESOLVED,
    CandidateQueue,
    ChatCandidate,
    ChatRegistry,
    MessageLike,
    RejectedLog,
    ResolvedChat,
    TelegramJoiner,
    why,
)
from sniffer.sources.telegram_discover_screen import screen

log = structlog.get_logger(__name__)


class ChatDiscovery:
    """Сбор и отбор кандидатов. В Telegram уходит только чтение.

    Вступления здесь нет вовсе — им занимается `ChatJoiner`. Разделение не
    косметическое: отбор идёт вместе с потоком сообщений и стоит одного
    `resolve_username`, вступление случается три раза в сутки и стоит аккаунта,
    если ошибиться.
    """

    def __init__(
        self,
        *,
        registry: ChatRegistry,
        queue: CandidateQueue,
        rejected: RejectedLog,
        client: TelegramJoiner,
        city: str = "",
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._rejected = rejected
        self._client = client
        self._city = city or get_settings().default_city

    async def harvest(self, messages: Iterable[MessageLike], found_in: str = "") -> int:
        """Собрать кандидатов из сообщений, которые и так через нас проходят.

        Возвращает число новых кандидатов в очереди.
        """
        added = 0
        for message in messages:
            for candidate in candidates_from(message, found_in):
                added += int(await self._consider(candidate))
        return added

    async def harvest_vocabulary(self, words: Sequence[str]) -> int:
        """Поиск по словарю через `contacts.SearchRequest`.

        `search_dialogs` для этого не годится: стабильно отваливается по
        таймауту (spec-v2, 7), а `contacts.SearchRequest` отрабатывает за
        секунды. Здесь чат приезжает уже разрешённым — второй `resolve` был бы
        лишним запросом ради данных, которые уже на руках.
        """
        added = 0
        for word in words:
            query = word.strip()
            if not query:
                continue
            try:
                found = await self._client.search_contacts(query, MAX_SEARCH_RESULTS)
            except Exception as exc:
                log.warning("discover.search_failed", query=query, error=why(exc))
                continue
            for chat in found:
                if not chat.username:
                    # Без username вступить нельзя и сослаться не на что.
                    continue
                candidate = ChatCandidate(
                    key=f"@{chat.username.lower()}",
                    username=chat.username,
                    found_in=f"search:{query}",
                )
                added += int(await self._consider(candidate, resolved=chat))
        return added

    async def _consider(
        self, candidate: ChatCandidate, resolved: ResolvedChat | None = None
    ) -> bool:
        """Отобрать кандидата. `True` — встал в очередь."""
        if await self._queue.is_queued(candidate.key):
            return False
        if await self._rejected.is_rejected(candidate.key):
            return False
        if candidate.username and await self._registry.has_chat(username=candidate.username):
            return False

        if resolved is None:
            resolved = await self._look(candidate)
        if resolved is None:
            await self._rejected.reject(candidate.key, REJECT_UNRESOLVED)
            return False
        # Сверка по id — только когда id известен. У непройденного приглашения
        # он нулевой: до вступления Telegram id закрытого чата не отдаёт, и
        # `has_chat(tg_id=0)` спросил бы про несуществующий чат.
        if resolved.tg_id and await self._registry.has_chat(tg_id=resolved.tg_id):
            return False

        reason = screen(resolved, city=self._city)
        if reason:
            await self._rejected.reject(candidate.key, reason)
            log.info("discover.rejected", candidate=candidate.key, reason=reason)
            return False
        await self._queue.push(candidate)
        log.info("discover.queued", candidate=candidate.key, title=resolved.title)
        return True

    async def _look(self, candidate: ChatCandidate) -> ResolvedChat | None:
        """Что Telegram расскажет о кандидате, не вступая.

        Две формы ссылки — два запроса на чтение, но одно значение на выходе:
        дальше отбор не различает, откуда пришли название и тип.
        """
        try:
            if candidate.invite_hash:
                return await self._client.check_invite(candidate.invite_hash)
            return await self._client.resolve_username(candidate.username)
        except Exception as exc:
            log.warning("discover.look_failed", candidate=candidate.key, error=why(exc))
            return None
