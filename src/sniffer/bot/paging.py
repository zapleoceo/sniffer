"""Страницы выдачи: снимок найденного, порядок без засилья одного автора, курсор.

«Ещё N» и «Показать все N» работают не по новому поиску, а по СНИМКУ: то, что клиент
уже видел, не должно поменяться под кнопкой, пока он листает. Повторный поиск вернул
бы другой порядок и показал бы те же карточки второй раз, а то и подменил бы лот,
ушедший из выдачи. В `callback_data` едет только короткий ключ снимка и смещение
(`pg:Ab3xYz9Q:more:5` — 19 байт из 64): сами карточки по проводу не ходят.

Снимок живёт в памяти процесса 48 часов, а при переполнении вытесняется тот, к которому
дольше всех не обращались. Срок — собственный выбор, а не свойство Telegram: Bot API не
ограничивает возраст сообщения для нажатия кнопки. После перезапуска бота кнопка честно
отвечает «выдача устарела», а не листает неизвестно что; хранилище в базе — следующий
шаг и прячется за `SnapshotStore`, так что показ о нём не узнает.

Антиповтор — курсор снимка: «Ещё» с чужим смещением (двойное нажатие, старая кнопка)
ничего не показывает. Деньги тут ни при чём: от повторного списания защищает журнал
квоты, а курсор спасает глаза.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sniffer.sources.base import RawItem

# Не больше двух карточек одного автора на пять мест (R4 §3.0): иначе первые
# бесплатные карточки уйдут одному агентству.
PER_SELLER = 2
# Потолок «показать все»: решение владельца №7 не принято, взят порог, при котором
# выдача ещё листается, а не пролистывается. Поиск по архиву всё равно отдаёт не больше сотни.
SHOW_ALL_CAP = 50
SNAPSHOT_TTL = timedelta(hours=48)
SNAPSHOT_CAPACITY = 500

EXPIRED = "Эта выдача устарела. Повторите поиск, и я покажу свежую."
ALREADY_SHOWN = "Эти карточки уже показаны выше."
TRY_AGAIN = "Не получилось показать. Нажмите ещё раз через минуту."


@dataclass(frozen=True, slots=True)
class MoreOffer:
    """Что нарисовать под страницей: ключ снимка, с какого места продолжать и сколько осталось."""

    token: str
    offset: int
    rest: int


@dataclass(slots=True)
class Snapshot:
    """Выдача как её видел клиент: порядок зафиксирован, `cursor` — первое непоказанное."""

    owner: int
    items: tuple[RawItem, ...]
    root: int | None
    cursor: int = 0


class SnapshotStore(Protocol):
    def put(self, snapshot: Snapshot) -> str: ...

    def get(self, token: str) -> Snapshot | None: ...


class MemorySnapshots:
    def __init__(
        self,
        *,
        ttl: timedelta = SNAPSHOT_TTL,
        capacity: int = SNAPSHOT_CAPACITY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ttl, self._capacity, self._clock = ttl, capacity, clock
        self._items: OrderedDict[str, tuple[datetime, Snapshot]] = OrderedDict()

    def put(self, snapshot: Snapshot) -> str:
        token = secrets.token_urlsafe(6)
        # Срок считает хранилище по своим часам: возраст снимка — его свойство, а не вызывающего.
        self._items[token] = (self._clock(), snapshot)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> Snapshot | None:
        entry = self._items.get(token)
        if entry is None:
            return None
        created, snapshot = entry
        if self._clock() - created > self._ttl:
            del self._items[token]
            return None
        # Прочитанный снимок — самый свежий: листаемую выдачу вытеснять последней.
        self._items.move_to_end(token)
        return snapshot


SNAPSHOTS: SnapshotStore = MemorySnapshots()


def seller_key(item: RawItem) -> str | None:
    """Автор лота как ключ группировки; `None` — автора не знаем и в одну кучу не сваливаем."""
    seller = item.raw.get("seller_id")
    if isinstance(seller, int) and not isinstance(seller, bool):
        return f"{item.source}:id:{seller}"
    name = " ".join(item.seller_name.split()).casefold()
    return f"{item.source}:name:{name}" if name else None


def diversify(items: Sequence[RawItem], *, per_seller: int = PER_SELLER) -> list[RawItem]:
    """Не больше `per_seller` лотов одного автора впереди; лишние — в хвост, а не в корзину.

    Выбрасывать нельзя: счёт «подходит N» обязан остаться честным, а «показать все»
    показывает все. Порядок остальных сохраняется (стабильно).
    """
    taken: dict[str, int] = {}
    front: list[RawItem] = []
    back: list[RawItem] = []
    for item in items:
        key = seller_key(item)
        if key is not None and taken.get(key, 0) >= per_seller:
            back.append(item)
            continue
        if key is not None:
            taken[key] = taken.get(key, 0) + 1
        front.append(item)
    return front + back


def more_label(count: int) -> str:
    return f"Ещё {count}"


def all_label(rest: int) -> str:
    if rest <= SHOW_ALL_CAP:
        return f"Показать все {rest}"
    return f"Показать {SHOW_ALL_CAP} из {rest}"
