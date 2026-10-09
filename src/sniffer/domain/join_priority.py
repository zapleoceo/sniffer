"""Порядок очереди вступлений с учётом класса превью - обратимо и без голодания.

Приоритет НИКОГО не отклоняет и не удаляет: он только прибавляет штраф к `priority`
кандидата и вычитает «возраст в очереди». Поэтому:

* выключили настройку - порядок снова `(priority, found_at)`, как до этой правки;
* любой кандидат со временем обгоняет свежих: возраст растёт без потолка, а штраф
  конечен. Это и есть защита от голодания: unknown и off_topic не ждут вечно.

Лимиты вступлений (10 в сутки, час паузы, FloodWait) живут в журнале и сюда не
заходят: этот модуль решает только, КТО следующий в очереди.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sniffer.domain.chat_preview import FOREIGN_CITY, OFF_TOPIC, RELEVANT


@dataclass(frozen=True, slots=True)
class JoinPriorityPolicy:
    unknown_penalty: int = 20
    low_penalty: int = 60
    aging_hours_per_point: int = 6


@dataclass(frozen=True, slots=True)
class QueueEntry:
    id: int
    priority: int
    found_at: datetime
    preview_class: str | None = None


def penalty(preview_class: str | None, policy: JoinPriorityPolicy) -> int:
    """relevant выше, unknown (и NULL) посередине, off_topic / foreign_city ниже."""
    if preview_class == RELEVANT:
        return 0
    if preview_class in (OFF_TOPIC, FOREIGN_CITY):
        return policy.low_penalty
    return policy.unknown_penalty


def effective_priority(entry: QueueEntry, now: datetime, policy: JoinPriorityPolicy) -> int:
    """Меньше - раньше. Возраст в очереди - целые баллы, без потолка (старение)."""
    waited_h = max(0.0, (now - entry.found_at).total_seconds() / 3600)
    credit = int(waited_h // max(1, policy.aging_hours_per_point))
    return entry.priority + penalty(entry.preview_class, policy) - credit


def order_queue(
    entries: list[QueueEntry], now: datetime, policy: JoinPriorityPolicy | None
) -> list[QueueEntry]:
    """Порядок разбора. `policy=None` - прежний: `(priority, found_at)`."""
    if policy is None:
        return sorted(entries, key=lambda e: (e.priority, e.found_at))
    return sorted(
        entries,
        key=lambda e: (effective_priority(e, now, policy), e.found_at, e.id),
    )
