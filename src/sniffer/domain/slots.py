"""Слоты мониторинга: кто из мониторингов клиента держит оплаченный слот.

Чистая арифметика без базы и часов: «сейчас» приходит параметром. Источник правды о
деньгах — журнал платежей; от него считается число слотов (`slots(user, now)` = число
живых подписок) и сроки каждого. Здесь они раскладываются по мониторингам.

Мониторинг клиента — строка подписки на ветку поиска, у неё порядок претензии
(`priority`, затем `id`). Слоты берут первые по порядку; кому слота не хватило, тот ждёт
(⌛), но НИЧЕГО не теряет: привязка, фильтр и курсор целы. Оплаченный срок слота лежит на
мониторинге как `expires_at` — готовый предикат права (`delivery.entitled`) его читает.
Раскладка пересчитывается при каждом событии, которое меняет деньги или порядок (платёж,
возврат, включение, перенос), а между событиями права гасит сама дата: пропущенный
пересчёт укорачивает доступ, но не удлиняет его.

Самый долгий срок достаётся самому старшему мониторингу: когда ближайшая подписка кончится,
выбывает младший, а не тот, ради которого платили дольше всех.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Monitor:
    """Мониторинг клиента: ветка поиска, порядок претензии, оплаченный срок."""

    id: int
    root: int
    priority: int
    # `None` — выдан без платежа (владелец, ручная выдача): слотов не занимает и не считается.
    expires_at: datetime | None
    is_active: bool = True

    def holds_slot(self, now: datetime) -> bool:
        """Слот оплачен в момент `now`. Равенство — уже просрочка, как в предикате права."""
        return self.expires_at is not None and self.expires_at > now


@dataclass(frozen=True, slots=True)
class SlotState:
    """Итог пересчёта: сколько слотов оплачено, сколько занято и кто вернулся в работу."""

    slots: int = 0
    holding: int = 0
    resumed: int = 0

    @property
    def free(self) -> int:
        return max(0, self.slots - self.holding)


class Outcome(StrEnum):
    """Что получилось, когда клиент нажал «Следить» на ветке."""

    ENABLE = "enable"
    ALREADY_ON = "already_on"
    NEEDS_SUBSCRIPTION = "needs_subscription"
    NO_FREE_SLOT = "no_free_slot"


def live_ends(period_ends: Iterable[datetime], now: datetime) -> list[datetime]:
    """Сроки живых подписок, самый долгий первым. Закончившиеся слота не дают."""
    return sorted((end for end in period_ends if end > now), reverse=True)


def _ordered(monitors: Iterable[Monitor]) -> list[Monitor]:
    return sorted(monitors, key=lambda monitor: (monitor.priority, monitor.id))


def assign_expiry(
    ends: Sequence[datetime], monitors: Iterable[Monitor], now: datetime
) -> dict[int, datetime]:
    """Новые сроки мониторингов: только те, что меняются. Выданные без платежа не трогаем.

    k-й по порядку получает k-й по долготе срок; кому срока не хватило, тому срок
    «сейчас» — а уже просроченный остаётся как был, история не переписывается.
    Слот достаётся только АКТИВНОМУ мониторингу: пауза и «Удалить поиск» (архив — та же
    пауза) освобождают его, иначе слот простаивал бы за поиском, который не следит, а
    следующий по порядку ждал бы зря. Вернуться в работу такой мониторинг может только
    через `decide_enable` — тем же путём, что и любой, кому слот нужно получить.
    """
    changes: dict[int, datetime] = {}
    index = 0
    for monitor in _ordered(monitors):
        current = monitor.expires_at
        if current is None:
            continue
        if monitor.is_active and index < len(ends):
            wanted = ends[index]
            index += 1
        else:
            wanted = min(current, now)
        if wanted != current:
            changes[monitor.id] = wanted
    return changes


def state_after(
    ends: Sequence[datetime], monitors: Sequence[Monitor], now: datetime
) -> tuple[SlotState, dict[int, datetime]]:
    """Раскладка целиком: новые сроки и итоговое состояние (сколько вернулось в работу)."""
    changes = assign_expiry(ends, monitors, now)
    resumed = sum(
        1
        for monitor in monitors
        if monitor.id in changes and not monitor.holds_slot(now) and changes[monitor.id] > now
    )
    holding = sum(
        1
        for monitor in monitors
        if monitor.expires_at is not None and changes.get(monitor.id, monitor.expires_at) > now
    )
    return SlotState(slots=len(ends), holding=holding, resumed=resumed), changes


def decide_enable(slots: int, monitors: Sequence[Monitor], root: int, now: datetime) -> Outcome:
    """«Следить» на ветке: включить, уже включено, нужна подписка или слоты заняты."""
    existing = next((monitor for monitor in monitors if monitor.root == root), None)
    if existing is not None and existing.expires_at is None:
        return Outcome.ALREADY_ON if existing.is_active else Outcome.ENABLE
    if existing is not None and existing.holds_slot(now):
        return Outcome.ALREADY_ON if existing.is_active else Outcome.ENABLE
    if slots <= 0:
        return Outcome.NEEDS_SUBSCRIPTION
    holding = sum(1 for monitor in monitors if monitor.holds_slot(now))
    return Outcome.ENABLE if holding < slots else Outcome.NO_FREE_SLOT


@dataclass(frozen=True, slots=True)
class MovePlan:
    """Перенос слота: кого понизить, кого поднять и до каких значений порядка."""

    demote_id: int
    demote_to: int
    promote_to: int
    promote_id: int | None


def plan_move(
    monitors: Sequence[Monitor], *, to_root: int, from_root: int, now: datetime
) -> MovePlan | None:
    """Слот уходит с `from_root` на `to_root`. `None` — перенос невозможен.

    Невозможен, когда слот с `from_root` не оплачен (нечего переносить), когда он и
    так на `to_root`, или когда это одна и та же ветка. Целевой мониторинг уже может
    существовать (ждёт слота) — тогда его только поднимают в порядке; нет — его заводит
    вызывающий, `promote_id` пуст.
    """
    if from_root == to_root:
        return None
    source = next((monitor for monitor in monitors if monitor.root == from_root), None)
    target = next((monitor for monitor in monitors if monitor.root == to_root), None)
    if source is None or not source.holds_slot(now):
        return None
    if target is not None and target.holds_slot(now):
        return None
    priorities = [monitor.priority for monitor in monitors]
    return MovePlan(
        demote_id=source.id,
        demote_to=max(priorities) + 1,
        promote_to=min(priorities) - 1,
        promote_id=None if target is None else target.id,
    )
