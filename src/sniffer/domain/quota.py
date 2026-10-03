"""Журнал показов: чистая часть квоты — сколько допустить и в каком периоде.

Единица квоты — уникальная карточка за период на аккаунт. Повторный показ той же
карточки в том же периоде ничего не стоит (другой поиск, уточнение, «искать
снова»); в новом периоде она снова платная. Потолок вычисляется в момент выдачи
(`plans.card_cap`), а прошлые выдачи не пересчитываются.

Здесь нет ввода-вывода: решение «что из запрошенного пустить» — функция от четырёх
чисел, а границы периода — функция от якоря и «сейчас». Хранилище, часы и право
на тариф приходят снаружи (`bot/quota.py`), поэтому каждое правило проверяется
обычным тестом без базы, а база проверяется тем, что исполняет то же решение под
блокировкой.

Арифметика периода — `quota_period.py` (вьетнамский календарь, границы от якоря).
Номер периода отсюда нужен только для хранения: CHECK базы требует, чтобы границы
совпадали с формулой «якорь + k месяцев», и номер `k` — её второй аргумент.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sniffer.domain.quota_period import VIETNAM, Period, add_months, period_containing

# Резерв без подтверждения держит карточку занятой не дольше этого. Процесс мог
# умереть между «записал» и «отправил»: без срока человек платил бы карточкой,
# которой не видел. Десять минут — с запасом на самую долгую отправку.
RESERVATION_TTL = timedelta(minutes=10)
# Предложение подписки — не чаще раза в сутки на человека: второй раз тем же
# текстом оно уже давление, а не информация.
OFFER_COOLDOWN = timedelta(hours=24)


class Channel(StrEnum):
    """Откуда карточка попала к человеку. Значения — те же, что в CHECK таблицы."""

    SEARCH = "search"
    DEFERRED = "deferred"
    MONITOR = "monitor"

    @property
    def spends_cap(self) -> bool:
        """Слежение платное само по себе и потолок карточек не тратит.

        Но в журнал оно пишется: карточка, присланная слежением, не должна
        повторно показываться поиском как «новая».
        """
        return self is not Channel.MONITOR


@dataclass(frozen=True, slots=True)
class Decision:
    """Что из запрошенного допущено. Порядок выдачи сохранён во всех трёх."""

    granted: tuple[int, ...]  # новые в периоде: списываются сейчас
    repeated: tuple[int, ...]  # уже были в периоде: бесплатно
    withheld: tuple[int, ...]  # новые, но не поместились в остаток
    remaining: int | None  # сколько новых ещё можно в периоде; None — без лимита


def unique(ids: Sequence[int]) -> tuple[int, ...]:
    """Без повторов, порядок первого вхождения: одна карточка в выдаче дважды — одна."""
    return tuple(dict.fromkeys(ids))


def decide(
    requested: Sequence[int], *, seen: Collection[int], used: int, limit: int | None
) -> Decision:
    """Сколько из `requested` допустить при (потолок, занято, уже виденные).

    Уже виденные в периоде проходят всегда и бесплатно — и при исчерпанном лимите:
    человек заплатил за них раньше. Новые берутся по порядку выдачи, но не больше
    остатка: «лучшие по рейтингу» достаются тому, кто платит, а не «первые попавшиеся».
    `used > limit` возможен (подписка кончилась посреди периода, а выдано было
    больше десяти) — это остаток ноль, а не отрицательный.
    """
    if used < 0:
        raise ValueError("занято не бывает отрицательным")
    if limit is not None and limit < 0:
        raise ValueError("потолок не бывает отрицательным")
    ids = unique(requested)
    already = frozenset(seen)
    repeated = tuple(i for i in ids if i in already)
    fresh = tuple(i for i in ids if i not in already)
    if limit is None:
        return Decision(granted=fresh, repeated=repeated, withheld=(), remaining=None)
    room = max(0, limit - used)
    granted = fresh[:room]
    return Decision(
        granted=granted,
        repeated=repeated,
        withheld=fresh[room:],
        remaining=room - len(granted),
    )


def numbered_period(anchor: datetime, now: datetime) -> tuple[int, Period]:
    """Номер и границы периода якоря, в который попадает `now`.

    Номер выводится из начала уже посчитанного периода, а не отдельной формулой:
    вторая формула разошлась бы с первой на каком-нибудь 31-м числе, а сверка
    «`add_months(якорь, k)` равно началу» ниже падает громко, а не врёт тихо.
    """
    period = period_containing(anchor, now)
    local_anchor, local_start = anchor.astimezone(VIETNAM), period.start.astimezone(VIETNAM)
    number = (local_start.year - local_anchor.year) * 12 + (local_start.month - local_anchor.month)
    if add_months(anchor, number) != period.start:
        raise ArithmeticError(f"номер периода {number} не сходится с его началом {period.start}")
    return number, period


@dataclass(frozen=True, slots=True)
class Claim:
    """Что просит у хранилища одна выдача."""

    user_id: int
    listing_ids: tuple[int, ...]
    channel: Channel
    now: datetime
    limit: int | None
    passport_root: int | None = None
    request_id: int | None = None


@dataclass(frozen=True, slots=True)
class Reserved:
    """Что хранилище записало: решение и период, под которым оно принято."""

    period_id: int
    period: Period
    decision: Decision


@dataclass(frozen=True, slots=True)
class Ticket:
    """Чем подтвердить или вернуть резерв, когда станет известно, дошла ли выдача."""

    user_id: int
    period_id: int
    granted: tuple[int, ...]
    shown: tuple[int, ...]
    request_id: int | None = None

    def split(self, delivered: Collection[int]) -> tuple[Ticket, Ticket]:
        """Билет на ушедшие карточки и билет на остальные: подтвердить первый, вернуть второй."""
        done = set(delivered)

        def part(keep: bool) -> Ticket:
            return Ticket(
                user_id=self.user_id,
                period_id=self.period_id,
                granted=tuple(i for i in self.granted if (i in done) is keep),
                shown=tuple(i for i in self.shown if (i in done) is keep),
                request_id=self.request_id,
            )

        return part(True), part(False)


@dataclass(frozen=True, slots=True)
class Admission:
    """Итог допуска для показа: что можно вывести, что нельзя и сколько осталось.

    Пустой (`Admission()`) — допускать было нечего: поиск ничего не нашёл.
    `limit` и `period_end` пусты у владельца и у слежения: строки остатка у них нет.
    """

    granted: tuple[int, ...] = ()
    repeated: tuple[int, ...] = ()
    withheld: tuple[int, ...] = ()
    remaining: int | None = None
    limit: int | None = None
    period_end: datetime | None = None
    ticket: Ticket | None = None

    @property
    def shown(self) -> tuple[int, ...]:
        return (*self.granted, *self.repeated)

    def allows(self, listing_id: int) -> bool:
        return listing_id in self.granted or listing_id in self.repeated


@dataclass(frozen=True, slots=True)
class Usage:
    """Сколько занято в текущем периоде и когда он кончится. `None` — период не начат."""

    used: int
    period_end: datetime | None


@dataclass(frozen=True, slots=True)
class Standing:
    """Положение аккаунта для `/plan`: занято, потолок, дата обновления."""

    used: int
    limit: int | None
    period_end: datetime | None
