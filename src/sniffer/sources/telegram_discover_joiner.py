"""Очередь вступлений: одно вступление за вызов, лимиты — из CLAUDE.md.

Отделено от отбора (`telegram_discover.py`) не ради размера файла. Это две
разные работы с разным ритмом: отбор идёт вместе с потоком сообщений и стоит
одного `resolve_username`, вступление случается три раза в сутки и стоит
аккаунта, если ошибиться. Их и запускать будут по-разному.

Ручное вступление отменено (roadmap, волна 4.5), так что этот класс —
единственный способ, каким проект набирает чаты. Отсюда три свойства.

- **Два действия и ни одним больше.** CLAUDE.md открыл закрытый список
  исключений из «юзербот только читает»: вступление и беззвучный режим. Всё
  остальное — отправка, реакция, отметка о прочтении, пересылка — остаётся
  запрещённым, и в типе `TelegramJoiner` этих методов просто нет.
- **Лимиты живут в БД и берутся одной транзакцией.** Три вступления в сутки,
  час паузы, `FloodWait` до конца суток, потолок числа чатов. Счётчик в памяти
  обнулялся бы каждым деплоем; проверка отдельно от записи давала бы двум
  воркерам пройти одни и те же ворота. Поэтому слот занимает `claim_slot`.
- **Одно вступление за вызов.** `join_next()` делает максимум один join и
  выходит. Цикл «вступаем, пока очередь не кончится» — это ровно то массовое
  вступление, за которое аккаунт отбирают (spec-v2, 6.1).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

import structlog
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from sniffer.config import get_settings
from sniffer.sources.telegram_discover_reference import (
    DISCOVERED_RANK,
    JOIN_PAUSE_JITTER,
    MAX_TRACKED_CHATS,
    MIN_JOIN_PAUSE,
    REJECT_JOIN_REFUSED,
    REJECT_JOIN_REQUEST_SENT,
    CandidateQueue,
    ChatCandidate,
    ChatRegistry,
    DiscoveredChat,
    JoinLedger,
    RejectedLog,
    TelegramJoiner,
    why,
)

log = structlog.get_logger(__name__)

Now = Callable[[], datetime]
Jitter = Callable[[], timedelta]

# Отказы, после которых точно известно: вступления не произошло. Только их
# можно считать «кандидат плохой» — вернуть слот и выбросить кандидата.
# Всё остальное (обрыв связи, таймаут, незнакомая ошибка) — неизвестный исход,
# и слот там остаётся потраченным.
CANDIDATE_REFUSED = (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    InviteHashInvalidError,
    InviteHashExpiredError,
    InviteHashEmptyError,
    ChannelPrivateError,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _jitter() -> timedelta:
    """Разброс паузы. Ровно час в ровно час — подпись автомата."""
    return timedelta(seconds=random.uniform(0, JOIN_PAUSE_JITTER.total_seconds()))  # noqa: S311


class ChatJoiner:
    """Разбор очереди вступлений. Зависимости обязательны — заглушек нет.

    Ни у реестра, ни у очереди, ни у журнала нет значения по умолчанию, и это
    не недоделка. Реализация в памяти выглядела бы рабочей и молча разрешала бы
    три вступления после каждого перезапуска: отказ, который замечают по бану
    аккаунта, а не по красному тесту. Нет слоя `db` — нет и вступлений.
    """

    def __init__(
        self,
        *,
        registry: ChatRegistry,
        queue: CandidateQueue,
        ledger: JoinLedger,
        rejected: RejectedLog,
        client: TelegramJoiner,
        city: str = "",
        max_tracked: int = MAX_TRACKED_CHATS,
        now: Now = _utcnow,
        jitter: Jitter = _jitter,
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._ledger = ledger
        self._rejected = rejected
        self._client = client
        self._city = city or get_settings().default_city
        # Потолок жёсткий: настройка вправе его понизить, но не поднять.
        self._max_tracked = max(1, min(max_tracked, MAX_TRACKED_CHATS))
        self._now = now
        self._jitter = jitter

    async def join_next(self) -> DiscoveredChat | None:
        """Одно вступление, если все ворота открыты. Иначе `None`."""
        now = self._now()
        event_id = await self._ledger.claim_slot(
            now, next_allowed_at=now + MIN_JOIN_PAUSE + self._jitter()
        )
        if event_id is None:
            await self._explain_refusal(now)
            return None

        if await self._registry.count() >= self._max_tracked:
            # Потолок достигнут: дальше только замена, не рост (CLAUDE.md).
            # Проверяем после занятия слота, а не до: слот и так пропускает
            # одного за раз, поэтому здесь гонки уже нет.
            await self._ledger.release_slot(event_id=event_id)
            log.warning("discover.chat_cap_reached", cap=self._max_tracked)
            return None

        candidate = await self._queue.reserve()
        if candidate is None:
            await self._ledger.release_slot(event_id=event_id)
            return None
        return await self._join_reserved(candidate, event_id, now)

    async def retry_mutes(self) -> int:
        """Догнать беззвучный режим там, где он не встал с первого раза."""
        done = 0
        for tg_id in await self._ledger.pending_mutes():
            if await self._mute(tg_id):
                done += 1
        return done

    async def _join_reserved(
        self, candidate: ChatCandidate, event_id: int, now: datetime
    ) -> DiscoveredChat | None:
        """Вступление в уже зарезервированного кандидата на занятом слоте."""
        try:
            tg_id = await self._join(candidate)
        except FloodWaitError as flood:
            # Флуд-лимит на join — предупреждение, а не задержка: Telegram
            # называет секунды, но продолжать через них означает подтвердить
            # ему, что мы автомат. Стоп до конца суток (CLAUDE.md). Кандидат
            # возвращается в очередь: он ни в чём не виноват.
            blocked_until = _end_of_day(now)
            await self._ledger.record_flood(event_id=event_id, blocked_until=blocked_until)
            await self._queue.release(candidate.key)
            log.warning(
                "discover.join_flood",
                candidate=candidate.key,
                asked_s=getattr(flood, "seconds", 0),
                blocked_until=blocked_until.isoformat(),
            )
            return None
        except InviteRequestSentError:
            # `ImportChatInvite` в группу «только по заявке» — это состоявшийся
            # исходящий запрос: Telegram его записал, и заявка теперь висит у
            # модератора. Поэтому слот НЕ возвращается, хотя чата мы не
            # получили: вернуть его значит разрешить ещё одну такую же попытку
            # через час, и суточный лимит из трёх запросов превращается в
            # двадцать четыре.
            #
            # Кандидат при этом выбрасывается насовсем — исход известен точно, в
            # отличие от обрыва связи ниже. Отбор такие группы отсекает заранее
            # по `request_needed`, так что сюда попадает только та, что стала
            # модерируемой между проверкой и вступлением.
            await self._queue.drop(candidate.key)
            await self._rejected.reject(candidate.key, REJECT_JOIN_REQUEST_SENT)
            log.warning("discover.join_request_sent", candidate=candidate.key)
            return None
        except CANDIDATE_REFUSED as exc:
            # Telegram сказал «такого нет» или «сюда нельзя»: вступления точно
            # не было, слот возвращаем, кандидата больше не разбираем.
            await self._ledger.release_slot(event_id=event_id)
            await self._queue.drop(candidate.key)
            await self._rejected.reject(candidate.key, REJECT_JOIN_REFUSED)
            log.info("discover.join_refused", candidate=candidate.key, error=why(exc))
            return None
        except Exception as exc:
            # Обрыв связи посреди вступления: прошло оно или нет — неизвестно.
            # Слот остаётся потраченным (недосчитать безопаснее, чем сделать
            # четвёртое), кандидат возвращается в очередь (терять его нельзя,
            # разбирать её больше некому). Повторный join в тот же чат для
            # Telegram идемпотентен, так что худший исход — потерянные сутки.
            await self._queue.release(candidate.key)
            log.warning("discover.join_unknown", candidate=candidate.key, error=why(exc))
            return None

        # Событие достраивается сразу после успеха: слот уже занят с самого
        # начала, поэтому падение здесь не даст четвёртого вступления — только
        # безымянную строку в журнале.
        await self._ledger.confirm_join(event_id=event_id, tg_id=tg_id, username=candidate.username)
        # Город берётся из настройки, и это честно ровно потому, что в очередь
        # кандидат попадает только через `screen()`, где маркер города найден в
        # названии или описании — по обеим формам ссылки, включая приглашение.
        # Второй путь в очередь — сид `002_seed_candidates.sql`, где город выбрал
        # владелец руками. Третьего нет: пока это так, запись не лжёт.
        chat = DiscoveredChat(
            tg_id=tg_id,
            username=candidate.username,
            title=candidate.username or candidate.key,
            city=self._city,
            search_rank=DISCOVERED_RANK,
        )
        await self._registry.add(chat)
        await self._queue.drop(candidate.key)
        log.info("discover.joined", chat=chat.username or tg_id, tg_id=tg_id)
        await self._mute(tg_id)
        return chat

    async def _explain_refusal(self, now: datetime) -> None:
        """Почему слот не дали. Отдельный запрос — только на отказе."""
        state = await self._ledger.state(now)
        log.info(
            "discover.join_not_allowed",
            joins_in_window=state.joins_in_window,
            next_allowed_at=_stamp(state.next_allowed_at),
            blocked_until=_stamp(state.blocked_until),
        )

    async def _join(self, candidate: ChatCandidate) -> int:
        if candidate.invite_hash:
            return await self._client.join_invite(candidate.invite_hash)
        return await self._client.join_public(candidate.username)

    async def _mute(self, tg_id: int) -> bool:
        """Беззвучный режим только для чатов нашего реестра.

        Проверка перед вызовом — не перестраховка: `UpdateNotifySettings`
        меняет настройки аккаунта-человека, и промах по чужому чату владелец
        обнаружит пропущенным сообщением, а не логом.

        Провал не откатывает вступление. Вступление стоило суточного лимита, а
        незаглушенный чат — это неудобство, которое чинится повтором.
        """
        if not await self._registry.has_chat(tg_id=tg_id):
            log.warning("discover.mute_outside_registry", tg_id=tg_id)
            return False
        try:
            await self._client.set_muted(tg_id)
        except Exception as exc:
            await self._ledger.record_mute_failure(tg_id=tg_id, error=why(exc))
            log.warning("discover.mute_failed", tg_id=tg_id, error=why(exc))
            return False
        await self._ledger.mark_muted(tg_id=tg_id)
        return True


def _end_of_day(now: datetime) -> datetime:
    """Полночь UTC следующего дня.

    Сутки берём в UTC, а не в местном времени: сервер живёт в UTC, а граница,
    которая ездит вместе с часовым поясом контейнера, — это лимит, который
    невозможно проверить.
    """
    return datetime.combine((now + timedelta(days=1)).date(), time.min, tzinfo=UTC)


def _stamp(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else ""
