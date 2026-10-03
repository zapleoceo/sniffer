"""Что нотифаер делает с исходом отправки: чистое решение, без ввода-вывода.

Отделено от `Delivery`, чтобы таблица «исход → действие» читалась и проверялась
целиком, а не восстанавливалась из цикла отправки. Сюда не приходит ни сессия, ни
часы: время передаёт вызывающий, и тот же вход всегда даёт то же решение.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sniffer.config import Settings
from sniffer.notifier.outcome import DETAIL_LIMIT, Failure, Kind

# Попыток на сообщение. Раньше было три по пятнадцать минут — и «третья означает,
# что клиент заблокировал бота». Теперь блокировку узнаёт 403, а попытки
# остаются для сети и 5xx, где шесть попыток с нарастающей паузой (1, 2, 4, 8, 16
# минут) покрывают те же полчаса, но реагируют на короткий сбой за минуту, а не
# за четверть часа.
MAX_ATTEMPTS = 6
# Потолок одного ожидания: экспонента без него через десять попыток дала бы сутки.
BACKOFF_CAP = timedelta(hours=1)
# Сколько молчим, когда сломан токен: терять сообщения нельзя, а «сломан» чинит
# человек. Короткая пауза не дерёт Bot API пустыми запросами каждые десять секунд.
SYSTEM_PAUSE = timedelta(minutes=5)


class Action(StrEnum):
    RETRY = "retry"  # вернуть в очередь с паузой; попытка засчитана
    GIVE_UP = "give_up"  # status=failed: Telegram отказал этому сообщению или попытки кончились
    BLOCK = "block"  # клиент недоступен: пометить его, остальную очередь отменить
    PAUSE = "pause"  # остановить нотифаер на время; сообщение не виновато, попытка не тратится


@dataclass(frozen=True, slots=True)
class Policy:
    max_attempts: int = MAX_ATTEMPTS
    backoff_base: timedelta = timedelta(minutes=1)
    backoff_cap: timedelta = BACKOFF_CAP
    system_pause: timedelta = SYSTEM_PAUSE
    # Срок годности строки очереди, от времени, на которое она назначена. Пока у
    # подписки есть право на слежение — сутки. Отмену по окончании подписки (шесть
    # часов) делает матчер (`MonitorRepository.cancel_lapsed`). Число повторено в
    # настройках (`outbox_ttl_h`), равенство сторожит тест.
    ttl: timedelta = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Verdict:
    action: Action
    until: datetime | None = None  # RETRY — когда повторить, PAUSE — когда снова слать
    note: str = ""  # что записать в `outbox.last_error`


def policy_from(settings: Settings) -> Policy:
    """Политика с числами из окружения: срок годности меняется конфигом, а не правкой кода."""
    return Policy(ttl=timedelta(hours=settings.outbox_ttl_h))


def backoff(policy: Policy, attempts_made: int) -> timedelta:
    """Пауза после неудачной попытки: удваивается, но не дольше потолка."""
    return min(policy.backoff_cap, policy.backoff_base * (1 << min(attempts_made, 30)))


def decide(failure: Failure, *, attempts: int, now: datetime, policy: Policy) -> Verdict:
    """`attempts` — сколько попыток у сообщения уже засчитано до этой."""
    note = f"{failure.reason}: {failure.detail}"[:DETAIL_LIMIT]
    match failure.kind:
        case Kind.BLOCKED:
            return Verdict(Action.BLOCK, note=note)
        case Kind.RATE_LIMITED:
            return Verdict(
                Action.PAUSE, until=now + timedelta(seconds=failure.retry_after), note=note
            )
        case Kind.SYSTEM:
            return Verdict(Action.PAUSE, until=now + policy.system_pause, note=note)
        case Kind.REJECTED:
            return Verdict(Action.GIVE_UP, note=note)
        case Kind.TRANSIENT:
            if attempts + 1 >= policy.max_attempts:
                return Verdict(
                    Action.GIVE_UP, note=f"attempts_exhausted: {failure.detail}"[:DETAIL_LIMIT]
                )
            return Verdict(Action.RETRY, until=now + backoff(policy, attempts), note=note)
