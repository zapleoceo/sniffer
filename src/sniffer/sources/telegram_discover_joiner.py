"""Очередь вступлений: одно вступление за вызов, лимиты — из CLAUDE.md.

Отделено от отбора (`telegram_discover.py`) не ради размера файла. Это две
разные работы с разным ритмом: отбор идёт вместе с потоком сообщений и стоит
одного `resolve_username`, вступление случается до десяти раз в скользящие сутки и стоит
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
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from sniffer.config import get_settings
from sniffer.sources.telegram_discover_reference import (
    DISCOVERED_RANK,
    FLOOD_MIN_STOP,
    JOIN_PAUSE_JITTER,
    MAX_TRACKED_CHATS,
    MIN_JOIN_PAUSE,
    REJECT_ALREADY_INSIDE,
    REJECT_ALREADY_MEMBER,
    REJECT_JOIN_REFUSED,
    REJECT_JOIN_REQUEST_SENT,
    REJECT_TOO_MANY_ATTEMPTS,
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

# Отказы, после которых точно известно: вступления не произошло, и кандидат
# больше не годится. Все шесть — ответы СЕРВЕРА, то есть запрос наружу уже
# состоялся, поэтому слот они НЕ возвращают.
#
# Возвращали. И это давало 24 запроса в сутки — столько, сколько влезает при
# часовой паузе, то есть лимит переставал существовать вовсе: `release_slot`
# удаляет событие, а вместе с ним исчезает и `next_allowed_at`, то есть пауза
# в час. Замер: очередь из плохих кандидатов, вызов раз в час — 24 исходящих
# `JoinChannel` за сутки; при остановленном времени — 40. Флуд-лимит Telegram
# считает запросы, а не успехи, и `ChannelPrivateError` для чата, закрывшегося
# за недели ожидания в очереди, — обычное дело.
#
# Принцип записан в spec-v2 4.5 и звучит однозначно: слот потрачен и тогда,
# когда чата мы не получили, но запрос наружу состоялся. Правка ревью закрыла
# один экземпляр этого класса (`InviteRequestSentError`) и оставила шесть.
CANDIDATE_REFUSED = (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    InviteHashInvalidError,
    InviteHashExpiredError,
    InviteHashEmptyError,
    ChannelPrivateError,
)

# Мы уже в этом чате. Исход известен точно, кандидат не нужен, но запрос
# наружу состоялся — значит слот потрачен, как у любого отказа сервера.
# Случай не редкий: очередь живёт неделями, и чат мог быть взят другим путём.
ALREADY_INSIDE = (UserAlreadyParticipantError,)

# Аккаунт упёрся в собственный потолок чатов Telegram. Само не пройдёт: пока
# из чатов не выйдут, каждая следующая попытка — запрос в пустоту. Поэтому
# останавливаемся так же, как после флуда, а не пробуем ещё десять раз в сутки.
ACCOUNT_FULL = (ChannelsTooMuchError,)

# Сколько неизвестных исходов подряд терпит один кандидат. Ограничение нужно НЕ
# для перечисленных выше ошибок, а для НЕПЕРЕЧИСЛЕННЫХ: неизвестный исход
# возвращает кандидата в очередь, а `reserve()` берёт наименьший приоритет —
# то есть его же. Замер до правки: `UserAlreadyParticipantError` давал 21
# исходящий join за семь суток, по три в день, вечно, и второй кандидат не
# получал хода никогда.
#
# Считать попытки, а не перечислять ошибки: список типов закрывает те случаи,
# которые вспомнили, а счётчик закрывает и седьмой, которого никто не назвал.
MAX_CANDIDATE_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _jitter() -> timedelta:
    """Разброс паузы. Ровно час в ровно час — подпись автомата."""
    return timedelta(seconds=random.uniform(0, JOIN_PAUSE_JITTER.total_seconds()))  # noqa: S311


class ChatJoiner:
    """Разбор очереди вступлений. Зависимости обязательны — заглушек нет.

    Ни у реестра, ни у очереди, ни у журнала нет значения по умолчанию, и это
    не недоделка. Реализация в памяти выглядела бы рабочей и молча разрешала бы
    десять вступлений после каждого перезапуска: отказ, который замечают по бану
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

        candidate = await self._fresh_candidate()
        if candidate is None:
            await self._ledger.release_slot(event_id=event_id)
            return None
        return await self._join_reserved(candidate, event_id, now)

    async def _fresh_candidate(self) -> ChatCandidate | None:
        """Первый кандидат, которого ещё НЕТ в нашем реестре.

        Кандидат на чат, в котором мы уже состоим, не должен стоить ни слота, ни
        запроса наружу: выяснить это можно у своего же реестра, молча, не
        спрашивая Telegram. Замер на живой базе 03.09.2026: в очереди стояли
        ДВАДЦАТЬ таких — ровно все чаты реестра, — то есть двое суток суточного
        бюджета уходило на вступление в то, что у нас уже есть.
        (`UserAlreadyParticipantError` такой кандидат выбрасывает и без этой
        проверки, но ценой одного исходящего запроса каждый — а Telegram считает
        именно запросы.)

        Пропускать их надо ЗДЕСЬ, а не отбором: сид и находки разведки кладут
        кандидата в очередь без `screen()`, а между отбором и вступлением
        проходят часы — за них чат мог попасть в реестр другим путём.

        Цикл ограничен размером реестра: больше уже-известных кандидатов, чем
        чатов в реестре, быть не может. Каждый шаг цикла — только чтение своей
        базы, ни одного запроса к Telegram.

        Приглашение (`+hash`) проверить нечем: до вступления Telegram id
        закрытого чата не отдаёт, а имени у него нет. Такой кандидат идёт как
        раньше — там от лишнего запроса защищает `ALREADY_INSIDE`.
        """
        for _ in range(self._max_tracked):
            candidate = await self._queue.reserve()
            if candidate is None:
                return None
            if not candidate.username:
                return candidate
            if not await self._registry.has_chat(username=candidate.username):
                return candidate
            await self._queue.drop(candidate.key)
            await self._rejected.reject(candidate.key, REJECT_ALREADY_MEMBER)
            log.info("discover.candidate_already_tracked", candidate=candidate.key)
        # Столько уже-известных подряд, сколько чатов в реестре: дальше искать
        # нечего, и слот вызывающий вернёт сам.
        log.warning("discover.queue_full_of_known_chats", checked=self._max_tracked)
        return None

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
            blocked_until = _flood_stop(now)
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
            # через час, и суточный лимит превращается в двадцать четыре
            # запроса — столько влезает в сутки при часовой паузе.
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
            # Telegram сказал «такого нет» или «сюда нельзя»: вступления не
            # было, кандидата больше не разбираем. Слот НЕ возвращаем — запрос
            # наружу состоялся, а лимит считает запросы (см. CANDIDATE_REFUSED).
            await self._queue.drop(candidate.key)
            await self._rejected.reject(candidate.key, REJECT_JOIN_REFUSED)
            log.info("discover.join_refused", candidate=candidate.key, error=why(exc))
            return None
        except ALREADY_INSIDE as exc:
            # Мы в этом чате и так. Кандидат не нужен, слот потрачен: запрос
            # ушёл, и Telegram на него ответил.
            await self._queue.drop(candidate.key)
            await self._rejected.reject(candidate.key, REJECT_ALREADY_INSIDE)
            log.info("discover.already_inside", candidate=candidate.key, error=why(exc))
            return None
        except ACCOUNT_FULL as exc:
            # Потолок чатов у самого Telegram: следующая попытка ничего не
            # изменит. Останавливаемся, как после флуда; кандидат ни в чём не
            # виноват и возвращается в очередь.
            blocked_until = _flood_stop(now)
            await self._ledger.record_flood(event_id=event_id, blocked_until=blocked_until)
            await self._queue.release(candidate.key)
            log.error(
                "discover.account_full",
                candidate=candidate.key,
                blocked_until=blocked_until.isoformat(),
                error=why(exc),
            )
            return None
        except Exception as exc:
            # Обрыв связи посреди вступления: прошло оно или нет — неизвестно.
            # Слот остаётся потраченным (недосчитать безопаснее, чем сделать
            # одиннадцатое), кандидат возвращается в очередь (терять его нельзя,
            # разбирать её больше некому). Повторный join в тот же чат для
            # Telegram идемпотентен, так что худший исход — потерянные сутки.
            #
            # Но возвращается он НЕ бесконечно: `reserve()` берёт наименьший
            # приоритет, то есть его же, и один вечный кандидат съедал все три
            # слота каждые сутки — навсегда, а остальная очередь не двигалась.
            # Поэтому попытки считаются, и после `MAX_CANDIDATE_ATTEMPTS` он
            # уходит в отклонённые. Так закрывается и седьмая ошибка, которую
            # никто не назвал: ограничение на попытки не зависит от её типа.
            attempts = await self._queue.release(candidate.key)
            if attempts >= MAX_CANDIDATE_ATTEMPTS:
                await self._queue.drop(candidate.key)
                await self._rejected.reject(candidate.key, REJECT_TOO_MANY_ATTEMPTS)
                log.warning(
                    "discover.candidate_exhausted",
                    candidate=candidate.key,
                    attempts=attempts,
                    error=why(exc),
                )
                return None
            log.warning(
                "discover.join_unknown",
                candidate=candidate.key,
                attempts=attempts,
                error=why(exc),
            )
            return None

        # Событие достраивается сразу после успеха: слот уже занят с самого
        # начала, поэтому падение здесь не даст одиннадцатого вступления — только
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


def _flood_stop(now: datetime) -> datetime:
    """До каких пор не вступаем после флуд-предупреждения.

    «До конца суток» само по себе оказалось лазейкой: флуд в 23:59 держал
    ШЕСТЬДЕСЯТ СЕКУНД, а дальше оставалась только часовая пауза — замер дал два
    новых вступления в течение трёх часов после предупреждения. Буква правила
    соблюдалась, защитный смысл исчезал.

    Поэтому берётся большее из двух: остаток суток и `FLOOD_MIN_STOP`. Флуд в
    полдень держит до полуночи, флуд в 23:59 — двенадцать часов, и короче
    полусуток стоп не бывает ни в какой час.
    """
    return max(_end_of_day(now), now + FLOOD_MIN_STOP)


def _end_of_day(now: datetime) -> datetime:
    """Полночь UTC следующего дня.

    Сутки берём в UTC, а не в местном времени: сервер живёт в UTC, а граница,
    которая ездит вместе с часовым поясом контейнера, — это лимит, который
    невозможно проверить.
    """
    return datetime.combine((now + timedelta(days=1)).date(), time.min, tzinfo=UTC)


def _stamp(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else ""
