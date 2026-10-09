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
from telethon.errors import FloodWaitError

from sniffer.config import get_settings
from sniffer.sources.telegram_discover_joiner import CANDIDATE_REFUSED
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

# Telethon при высокоуровневом разборе имени отвечает не RPC-ошибкой, а
# `ValueError` с этими словами. Текст — единственный признак, поэтому сверка по
# подстроке и только для `ValueError`: чужой `ValueError` (баг у нас) «чата
# нет» не означает.
_NOT_FOUND_TEXTS = ("No user has", "Cannot find any entity corresponding to")


def chat_does_not_exist(exc: Exception) -> bool:
    """Telegram ОТВЕТИЛ, что такого чата или имени нет.

    Единственный исход, который вправе записаться в отказы как `unresolved`:
    ответ стабилен, и повторный вопрос ничего не изменит. Всё остальное — сеть,
    таймаут, `FloodWait`, сбой RPC, неизвестное исключение — говорит о нашем
    соединении, а не о кандидате. Набор «отказов сервера» один на разведку и
    вступление (`CANDIDATE_REFUSED`): два списка разошлись бы.
    """
    if isinstance(exc, CANDIDATE_REFUSED):
        return True
    return isinstance(exc, ValueError) and any(text in str(exc) for text in _NOT_FOUND_TEXTS)


class ChatDiscovery:
    """Сбор и отбор кандидатов. В Telegram уходит только чтение.

    Вступления здесь нет вовсе — им занимается `ChatJoiner`. Разделение не
    косметическое: отбор идёт вместе с потоком сообщений и стоит одного
    `resolve_username`, вступление случается до десяти раз в скользящие сутки и стоит аккаунта,
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
        # После FloodWait в этом проходе больше не спрашиваем: каждый следующий
        # запрос под флуд-лимитом — тот же ретрай в цикле, только по другим
        # кандидатам. Кандидаты не теряются, их встретят снова.
        self._flooded = False

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
        if candidate.username:
            if await self._excluded(candidate, username=candidate.username):
                return False
            if await self._registry.has_chat(username=candidate.username):
                return False

        if resolved is None:
            if self._flooded:
                return False
            try:
                resolved = await self._look(candidate)
            except Exception as exc:
                # `Exception`, а не корень: Ctrl+C и отмена задачи — не отказ
                # запроса и должны дойти наверх. Отказ запроса НЕ превращается
                # в `unresolved`: 18.09.2026 за час так записали 154 из 198
                # отказов, и кандидаты, о которых Telegram ничего не сказал,
                # пропали из очереди навсегда.
                if not chat_does_not_exist(exc):
                    self._flooded = self._flooded or isinstance(exc, FloodWaitError)
                    log.warning(
                        "discover.look_failed",
                        candidate=candidate.key,
                        error_type=type(exc).__name__,
                        error=why(exc),
                    )
                    return False
                resolved = None
        if resolved is None:
            await self._rejected.reject(candidate.key, REJECT_UNRESOLVED)
            return False
        # Сверка по id — только когда id известен. У непройденного приглашения
        # он нулевой: до вступления Telegram id закрытого чата не отдаёт, и
        # `has_chat(tg_id=0)` спросил бы про несуществующий чат.
        if resolved.tg_id:
            if await self._excluded(candidate, tg_id=resolved.tg_id):
                return False
            if await self._registry.has_chat(tg_id=resolved.tg_id):
                return False

        reason = screen(resolved, city=self._city)
        if reason:
            await self._rejected.reject(candidate.key, reason)
            log.info("discover.rejected", candidate=candidate.key, reason=reason)
            return False
        await self._queue.push(candidate)
        log.info("discover.queued", candidate=candidate.key, title=resolved.title)
        return True

    async def _excluded(
        self, candidate: ChatCandidate, *, tg_id: int | None = None, username: str = ""
    ) -> bool:
        """Ссылка на чат, который владелец исключил: кандидата не заводим (019).

        Не пишем в отклонённые: исключение обратимо, а запись в `chat_rejects` пережила бы
        возврат чата и стала бы вторым источником правды о нём.
        """
        if not await self._registry.is_excluded(tg_id=tg_id, username=username):
            return False
        log.info("discover.candidate_excluded", candidate=candidate.key, tg_id=tg_id)
        return True

    async def _look(self, candidate: ChatCandidate) -> ResolvedChat | None:
        """Что Telegram расскажет о кандидате, не вступая.

        Две формы ссылки — два запроса на чтение, но одно значение на выходе:
        дальше отбор не различает, откуда пришли название и тип.
        """
        if candidate.invite_hash:
            return await self._client.check_invite(candidate.invite_hash)
        return await self._client.resolve_username(candidate.username)
