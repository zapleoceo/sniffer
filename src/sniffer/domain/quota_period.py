"""Периоды квоты: «годовщина» от якоря с календарными месяцами.

Бесплатные карточки считаются за период, а не за календарный месяц: человек,
пришедший 28-го, не получает новую десятку через три дня, а человек, пришедший
1-го, не теряет её 31-го. Период аккаунта начинается в момент, когда у него
впервые списалась квота (якорь), и длится ровно календарный месяц: якорь +
k месяцев, с обрезкой дня по длине месяца.

Каждая граница считается ОТ ЯКОРЯ, а не от предыдущей границы: иначе обрезка
накапливалась бы (31 янв → 28 фев → 28 мар), и день уходил бы навсегда. От
якоря 31 января границы такие: 28 февраля, 31 марта, 30 апреля, 31 мая.

Время только UTC и только с часовым поясом: граница, посчитанная в местном
времени, сдвигалась бы на час-два и менялась бы от сервера к серверу, а
«точность расчёта» — требование владельца. Наивный момент — это баг вызывающего,
и здесь он падает громко, а не угадывается.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Period:
    """Полуинтервал [start, end): конец уже принадлежит следующему периоду."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= _utc(moment) < self.end


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("момент квоты обязан быть с часовым поясом")
    return moment.astimezone(UTC)


def add_months(moment: datetime, months: int) -> datetime:
    """Тот же момент через `months` календарных месяцев; день обрезается по длине месяца."""
    moment = _utc(moment)
    index = moment.year * 12 + (moment.month - 1) + months
    year, month_zero = divmod(index, 12)
    month = month_zero + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def period_containing(anchor: datetime, now: datetime) -> Period:
    """Период якоря, в который попадает `now`.

    Момент раньше якоря — расхождение часов между процессами на миллисекунды:
    считаем его первым периодом, а не роняем списание квоты.
    """
    anchor, now = _utc(anchor), _utc(now)
    if now < anchor:
        return Period(anchor, add_months(anchor, 1))
    months = (now.year - anchor.year) * 12 + (now.month - anchor.month)
    if add_months(anchor, months) > now:
        months -= 1
    return Period(add_months(anchor, months), add_months(anchor, months + 1))
