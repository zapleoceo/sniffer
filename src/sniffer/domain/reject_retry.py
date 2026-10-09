"""Правила адресного повтора отказа: можно ли, когда и сколько раз.

Чистая функция без ввода-вывода: репозиторий собирает факты (`RetryFacts`), правило
отвечает (`RetryDecision`), дашборд только показывает ответ и мапит код на HTTP-статус.
Одна и та же `evaluate` стоит и за кнопкой на странице, и за проверкой на сервере под
замком, поэтому «кнопка выключена, а сервер пустил» (или наоборот) невозможно по
построению: второй копии правила нет.

Повторяется ТОЛЬКО временный отказ (`RejectClass.TEMPORARY`, сегодня это один
`too_many_attempts`): сбой, а не суждение о чате. Постоянный, «уже участник» и «ждём
модератора» повтором не лечатся; «неизвестно» нельзя повторять вслепую, потому что по
записи не различить «чата нет» и «сбой сети» (ограниченная повторная диагностика —
следующий шаг, в этот код не входит).

Лимиты вступлений joiner (10 в сутки, час паузы, FloodWait-стоп) здесь не дублируются и
не заменяются: повтор лишь возвращает ключ в очередь, а выпустит его из очереди тот же
joiner по своим правилам. Свои числа этого модуля — страховка от того, что владелец
нажмёт кнопку на всём списке.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sniffer.domain import reject_reasons
from sniffer.domain.reject_reasons import RejectClass

# Между двумя попытками на один ключ. Сутки: joiner выпускает из очереди не быстрее
# десятка в сутки, и повтор раньше, чем кандидат хотя бы мог дойти до вступления, —
# это второй круг того же сбоя.
COOLDOWN = timedelta(hours=24)

# Сколько попыток на ключ за всё время. Ключ, который трижды вернулся с тем же
# отказом, не сбой, а свойство чата: дальше только руками.
MAX_RETRIES_PER_KEY = 3

# Запросов повтора в скользящие сутки на всю систему. Совпадает с суточным лимитом
# вступлений по размеру, но НЕ с ним по смыслу: счётчик свой, лимиты joiner не трогает.
MAX_RETRIES_PER_DAY = 10
RETRY_WINDOW = timedelta(hours=24)


class RetryCode(StrEnum):
    OK = "ok"
    # Класс отказа не допускает повтора.
    PERMANENT = "permanent"
    MEMBERSHIP = "membership"
    PENDING = "pending"
    UNKNOWN = "unknown"
    # Временный, но рано или слишком часто.
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    COOLDOWN = "cooldown"
    FLOOD_STOP = "flood_stop"
    DAILY_LIMIT = "daily_limit"
    # Исходы, которые знает только репозиторий (нужна база, чтобы их увидеть).
    NOT_FOUND = "not_found"
    ALREADY_QUEUED = "already_queued"
    BAD_KEY = "bad_key"


@dataclass(frozen=True, slots=True)
class RetryFacts:
    """Всё, что правилу надо знать. Время приходит снаружи — правило детерминировано."""

    reason: str
    now: datetime
    attempts_total: int
    last_requested_at: datetime | None
    used_in_window: int
    oldest_in_window: datetime | None
    blocked_until: datetime | None


@dataclass(frozen=True, slots=True)
class RetryDecision:
    code: RetryCode
    message: str
    # Когда условие снимется (cooldown, стоп, суточное окно); для «никогда» — пусто.
    available_at: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.code is RetryCode.OK


_BY_CLASS: dict[RejectClass, tuple[RetryCode, str]] = {
    RejectClass.PERMANENT: (
        RetryCode.PERMANENT,
        "постоянный отказ — суждение о самом чате, повтор ничего не изменит",
    ),
    RejectClass.MEMBERSHIP: (
        RetryCode.MEMBERSHIP,
        "мы уже в этом чате — вступать некуда",
    ),
    RejectClass.PENDING: (
        RetryCode.PENDING,
        "ждём модератора или нужен шаг человека — слепой повтор бессмыслен",
    ),
    RejectClass.UNKNOWN: (
        RetryCode.UNKNOWN,
        "неизвестно: не различить отсутствие чата и сбой",
    ),
}


def not_retryable(reason: str) -> RetryDecision | None:
    """Ответ для класса, который повтором не лечится; для временного — `None`."""
    kind = reject_reasons.classify(reason)
    if kind is RejectClass.TEMPORARY:
        return None
    code, message = _BY_CLASS[kind]
    return RetryDecision(code, message)


def evaluate(facts: RetryFacts) -> RetryDecision:
    """Порядок проверок важен: от «никогда» к «позже», суточный лимит последним.

    Суточный лимит стоит последним, чтобы 429 получал только тот запрос, который иначе
    прошёл бы: отказ по существу (409) честнее «приходите завтра».
    """
    refusal = not_retryable(facts.reason)
    if refusal is not None:
        return refusal

    if facts.attempts_total >= MAX_RETRIES_PER_KEY:
        return RetryDecision(
            RetryCode.ATTEMPTS_EXHAUSTED,
            f"исчерпано попыток на ключ: {facts.attempts_total} из {MAX_RETRIES_PER_KEY}",
        )

    if facts.last_requested_at is not None:
        ready = facts.last_requested_at + COOLDOWN
        if facts.now < ready:
            return RetryDecision(
                RetryCode.COOLDOWN,
                "между попытками на один ключ должно пройти "
                f"{int(COOLDOWN.total_seconds() // 3600)} ч",
                available_at=ready,
            )

    if facts.blocked_until is not None and facts.now < facts.blocked_until:
        return RetryDecision(
            RetryCode.FLOOD_STOP,
            "вступления остановлены после FloodWait — повтор не раньше конца стопа",
            available_at=facts.blocked_until,
        )

    if facts.used_in_window >= MAX_RETRIES_PER_DAY:
        frees = facts.oldest_in_window + RETRY_WINDOW if facts.oldest_in_window else None
        return RetryDecision(
            RetryCode.DAILY_LIMIT,
            f"лимит повторов исчерпан: {facts.used_in_window} из {MAX_RETRIES_PER_DAY} "
            "за скользящие сутки",
            available_at=frees,
        )

    return RetryDecision(RetryCode.OK, "можно повторить")


@dataclass(frozen=True, slots=True)
class RetryOffer:
    """Что показать у отказа: решение правила и сколько попыток на ключ уже было."""

    decision: RetryDecision
    attempts_used: int = 0


def next_retry_at(now: datetime) -> datetime:
    """Раньше этого момента новую попытку на ключ не принимаем."""
    return now + COOLDOWN


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """Строка журнала попыток: снимок исходного отказа и что с ним стало."""

    id: int
    reject_key: str
    reject_reason: str
    reject_rejected_at: datetime | None
    requested_at: datetime
    requested_by: int
    status: str
    outcome: str | None
    next_retry_at: datetime


class RetryStatus(StrEnum):
    CREATED = "created"
    # Та же форма отправлена повторно: вторая попытка не заведена, ответ тот же.
    REPLAYED = "replayed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class RetryResult:
    status: RetryStatus
    decision: RetryDecision
    record: RetryRecord | None = None
