"""Периоды квоты: «годовщина» от якоря с календарными месяцами.

Бесплатные карточки считаются за период, а не за календарный месяц: человек,
пришедший 28-го, не получает новую десятку через три дня, а человек, пришедший
1-го, не теряет её 31-го. Период аккаунта начинается в момент, когда у него
впервые списалась квота (якорь), и длится ровно календарный месяц: якорь +
k месяцев, с обрезкой дня по длине месяца.

Каждая граница считается ОТ ЯКОРЯ, а не от предыдущей границы: иначе обрезка
накапливалась бы (31 янв → 28 фев → 28 мар), и день уходил бы навсегда. От
якоря 31 января границы такие: 28 февраля, 31 марта, 30 апреля, 31 мая.

Календарь — вьетнамский (UTC+7): клиенты живут во Вьетнаме, и «месяц» для них
считается по местным числам. Арифметика в UTC давала бы странное: пришёл 31
октября в два часа ночи по местному времени (30 октября 19:00 UTC) — граница
выпала бы на 1 декабря по местному, хотя «месяц спустя» — 30 ноября. У Вьетнама
нет перехода на летнее время, поэтому фиксированный сдвиг даёт тот же результат,
что и база часовых поясов, и не требует `tzdata` на сервере. Хранится и
возвращается всё равно UTC: граница — это момент, а не запись о поясе.

Момент без часового пояса — баг вызывающего, и здесь он падает громко, а не
угадывается: «точность расчёта» — требование владельца.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo

VIETNAM = timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")


@dataclass(frozen=True, slots=True)
class Period:
    """Полуинтервал [start, end) в UTC: конец уже принадлежит следующему периоду."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= _utc(moment) < self.end


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("момент квоты обязан быть с часовым поясом")
    return moment.astimezone(UTC)


def add_months(moment: datetime, months: int, zone: tzinfo = VIETNAM) -> datetime:
    """Тот же момент через `months` календарных месяцев по календарю `zone`.

    День обрезается по длине месяца, время суток сохраняется; результат в UTC.
    """
    local = _utc(moment).astimezone(zone)
    index = local.year * 12 + (local.month - 1) + months
    year, month_zero = divmod(index, 12)
    month = month_zero + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(UTC)


def period_containing(anchor: datetime, now: datetime, zone: tzinfo = VIETNAM) -> Period:
    """Период якоря, в который попадает `now`.

    Момент раньше якоря — расхождение часов между процессами на миллисекунды:
    считаем его первым периодом, а не роняем списание квоты.
    """
    anchor, now = _utc(anchor), _utc(now)
    if now < anchor:
        return Period(anchor, add_months(anchor, 1, zone))
    local_anchor, local_now = anchor.astimezone(zone), now.astimezone(zone)
    months = (local_now.year - local_anchor.year) * 12 + (local_now.month - local_anchor.month)
    if add_months(anchor, months, zone) > now:
        months -= 1
    return Period(add_months(anchor, months, zone), add_months(anchor, months + 1, zone))
