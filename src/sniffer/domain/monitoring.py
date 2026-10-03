"""Слот слежения: сутки по Вьетнаму, отбор под потолок, счёт «ещё N», порядок слотов.

Чистые функции, ни одной строки ввода-вывода: «сколько ещё влезет сегодня» и «кому из
слотов положено работать» проверяются обычным тестом без базы, а агент (`worker/monitor.py`)
только исполняет решение.

Сутки считаются от полуночи по Вьетнаму (UTC+7), как дайджест, тихие часы и период квоты:
граница, по которой у клиента «обнуляется» потолок, не вправе зависеть от пояса сервера.
Старые 00:00 UTC падали на 07:00 утра клиента (D11).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from sniffer.domain.quota_period import VIETNAM

# Вид служебной строки очереди: сводка «подошло ещё N» сверх суточного потолка слота. Знают
# двое — монитор, который её ставит, и нотифаер, который её рисует, — поэтому живёт здесь.
OVERFLOW_KIND = "monitor_more"
# Сколько слот может простоять без права, чтобы после возвращения не прыгать курсором.
HEAD_JUMP_AFTER_HOURS = 24


class _HasId(Protocol):
    @property
    def id(self) -> int | None: ...


def local_day(now: datetime) -> date:
    """Вьетнамская дата момента."""
    return now.astimezone(VIETNAM).date()


def local_day_start(now: datetime) -> datetime:
    """Полночь вьетнамских суток, в которые попадает `now` (с поясом)."""
    return now.astimezone(VIETNAM).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class Pick[T: _HasId]:
    """Что из кандидатов уходит клиенту и сколько не вошло."""

    chosen: list[T]
    overflow: int


def pick[T: _HasId](candidates: Sequence[T], *, room: int) -> Pick[T]:
    """Под потолок берутся НОВЕЙШИЕ, а не старейшие: слежение — поток, а не очередь.

    Прежний порядок «старые первыми» (D1) на широком запросе слал утром пачку вчерашнего,
    а свежее не доходило до полуночи. Выбранное возвращается по возрастанию `id`: клиент
    читает ленту в том порядке, в каком объявления появлялись.
    """
    ordered = sorted(candidates, key=lambda item: item.id or 0, reverse=True)
    room = max(room, 0)
    chosen = sorted(ordered[:room], key=lambda item: item.id or 0)
    return Pick(chosen=chosen, overflow=len(ordered) - len(chosen))


@dataclass(frozen=True, slots=True)
class Overflow:
    """Счёт «подошло сверх потолка» за вьетнамские сутки и признак, что о нём сказали."""

    day: date | None = None
    count: int = 0
    notified: bool = False


def add_overflow(previous: Overflow, *, today: date, extra: int) -> tuple[Overflow, bool]:
    """Прибавить отброшенное и сказать, пора ли ставить сводку «ещё N».

    Сутки сменились — счёт начинается заново, и признак «сказали» тоже: сводка раз в сутки,
    а не раз в жизни слота. Сводка ставится на ПЕРВОЕ отброшенное за сутки; следующие лишь
    уточняют число в ещё не ушедшем сообщении (`DeliveryRepository.bump_overflow_notice`).
    """
    base = previous if previous.day == today else Overflow(day=today)
    if extra <= 0:
        return base, False
    return Overflow(today, base.count + extra, True), not base.notified


def open_slot_ids(ranked: Sequence[int], slots: int | None) -> set[int]:
    """Какие слоты клиента работают: первые `slots` по порядку, остальные стоят (no_slot).

    `ranked` — id подписок клиента, у которых есть право и которые не на ручной паузе, в
    порядке приоритета. `slots=None` — число слотов неизвестно (биллинг слотов не
    подключён): ограничения нет, работают все. Ничего не удаляется: слот без права лишь
    не сканируется, привязка, курсор и история целы.
    """
    if slots is None:
        return set(ranked)
    return set(ranked[: max(slots, 0)])


def must_jump_to_head(paused_since: datetime | None, now: datetime) -> bool:
    """Слот долго стоял без права: при возвращении курсор прыгает к «сейчас».

    Подписка обещает НОВОЕ. Вернувшийся после недели паузы слот не должен вываливать то,
    что накопилось, пока он стоял. Короткая пауза (поздний платёж) курсор не двигает.
    """
    if paused_since is None:
        return False
    return (now - paused_since).total_seconds() > HEAD_JUMP_AFTER_HOURS * 3600
