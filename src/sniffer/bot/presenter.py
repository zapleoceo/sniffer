"""Показ выдачи: что именно уходит клиенту, когда поиск закончился.

Здесь только представление результата — заголовок, карточки, кнопки. Когда его
показывать и что делать дальше решает `conversation.py`; слова берутся из
`wording`, одна карточка рисуется в `cards`, разметку кнопок строит `keyboards`.
Этот файл ничего не ищет и никуда не ходит.

Отдельным модулем, потому что выдача была второй ответственностью
`Conversation._search`. Первая — ход: «понял», поиск, ошибка, запись в журнал;
вторая — какими словами и с какими кнопками показать итог. Они меняются по
разным причинам, и правка заголовка не должна лежать в одном файле с порядком
шагов. Заодно у выдачи появилось место для правил, которым в ходе диалога
нечего делать: сколько карточек положено показать этому человеку.

`Reply` живёт здесь, потому что он и есть единица показа — одно сообщение.
`conversation` его ре-экспортирует: хендлеры, клавиатуры и симулятор
импортируют `Reply` оттуда с первого дня, и переезд не должен ломать их.

Сам по базе и сети не ходит и ходить не должен: выдача — функция от итога поиска
и паспорта, поэтому проверяется обычным тестом без Postgres и Telegram.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sniffer.bot import wording, wording_plan
from sniffer.bot.billing_wording import OFFER
from sniffer.bot.cards import render_cards
from sniffer.config import get_settings
from sniffer.domain.dialogue import Option, Question, feedback_buttons
from sniffer.domain.passport import Passport
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD
from sniffer.domain.quota import Admission
from sniffer.sources.base import RawItem


@dataclass(frozen=True, slots=True)
class Reply:
    """Одно сообщение клиенту. Кнопки описаны доменом, рисует их `keyboards`."""

    text: str
    question: Question | None = None
    feedback: tuple[Option, ...] = field(default_factory=tuple)
    # Предложить слежение за новыми объявлениями. Признак, а не готовая кнопка:
    # домен решает «уместно ли», разметку рисует `keyboards`.
    offer_subscription: bool = False
    passport_root: int | None = None
    # Предложить платный план («Подписка — 10 ⭐/мес»). Отдельно от `offer_subscription`:
    # то про слежение за темой, это про лимит карточек, и кнопки у них разные.
    offer_plan: bool = False


class Results(Protocol):
    """Итог поиска в том виде, в каком его читает показ.

    Протокол, а не `conversation.Found`: тот живёт в модуле, который сам
    импортирует этот, и обратный импорт замкнул бы цикл. Читать нужно ровно три
    поля, поэтому и описаны ровно они.
    """

    @property
    def items(self) -> Sequence[RawItem]: ...

    @property
    def status(self) -> str | None: ...

    @property
    def deferred(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Gate:
    """Что решила квота по этой выдаче: что можно показать и можно ли предлагать подписку.

    `shown` — карточки страницы, допущенные к показу, в порядке выдачи. К ним
    относятся и те, которых в журнале опознать не удалось: их показываем, но не
    считаем (источник без `listing_id`, см. `showing.py`). `offer` — право предложить
    подписку занято этим показом: не чаще раза в сутки на человека.
    """

    admission: Admission
    shown: tuple[RawItem, ...]
    offer: bool = False


def present(
    passport: Passport, found: Results, *, root: int | None, gate: Gate | None = None
) -> Reply:
    """Итог поиска одним сообщением: выдача, пустой ответ или «жду сбора».

    `root` — корень ветки: он едет в кнопках, чтобы старая выдача действовала на
    свой поиск, а не на тот, что выбран позже.
    """
    if found.items:
        if gate is None:
            return _results(passport, found, root)
        return _gated(passport, found, root, gate)
    if found.deferred:
        # Сбор поставлен в очередь, ответ придёт отдельным сообщением. Слежение
        # здесь не предлагаем: человек ещё не увидел, что искать «больше негде».
        return Reply(found.status or wording.SEARCH_FAILED)
    # Пустая выдача — самый честный повод предложить слежение: искать больше
    # негде, а новое появится.
    text = found.status or wording.nothing_found(passport)
    return Reply(f"{text}\n\n{OFFER}", offer_subscription=True, passport_root=root)


def _results(passport: Passport, found: Results, root: int | None) -> Reply:
    # Число считается здесь один раз и идёт и в заголовок, и в карточки: разное
    # число в двух местах дало бы «показываю 5» над четырьмя карточками.
    shown = min(len(found.items), get_settings().max_cards)
    header = wording.result_header(passport, len(found.items), shown)
    if found.status:
        header = f"{found.status}\n\n{header}"
    return Reply(
        f"{header}\n\n{render_cards(found.items, limit=shown)}",
        feedback=feedback_buttons(passport),
        offer_subscription=True,
        passport_root=root,
    )


def _gated(passport: Passport, found: Results, root: int | None, gate: Gate) -> Reply:
    """Выдача через квоту: остаток, допущенные карточки и честная строка про остальное."""
    if not gate.shown:
        return _exhausted(found, root, gate)
    admission = gate.admission
    shown = len(gate.shown)
    parts = [found.status] if found.status else []
    balance = wording_plan.balance_line(admission.limit, admission.remaining, admission.period_end)
    if balance:
        parts.append(balance)
    parts.append(wording.result_header(passport, len(found.items), shown))
    text = "\n\n".join(parts) + "\n\n" + render_cards(gate.shown, limit=shown)
    if admission.withheld:
        text += "\n\n" + wording_plan.more_line(
            len(found.items) - shown, limit=admission.limit, renews=admission.period_end
        )
    return Reply(
        text, feedback=feedback_buttons(passport), offer_subscription=True, passport_root=root
    )


def _exhausted(found: Results, root: int | None, gate: Gate) -> Reply:
    """Ни одной карточки показать нельзя: само сообщение — предложение или короткий ответ."""
    admission, total = gate.admission, len(found.items)
    if admission.limit != FREE_CARDS_PER_PERIOD:
        # Потолок подписчика: подписка ничего не добавит, поэтому ни кнопки, ни продажи.
        return Reply(
            wording_plan.exhausted_cap(total=total, renews=admission.period_end), passport_root=root
        )
    if gate.offer:
        return Reply(
            wording_plan.exhausted_offer(total=total, renews=admission.period_end),
            offer_plan=True,
            passport_root=root,
        )
    # Предложение сегодня уже было: второй раз тот же текст с кнопкой — давление.
    return Reply(
        wording_plan.exhausted_short(total=total, renews=admission.period_end), passport_root=root
    )


def present_offer(gate: Gate, *, root: int | None) -> Reply | None:
    """Отдельное сообщение-предложение после выдачи, часть которой лимит не пустил.

    Одно и без давления. Когда показать нечего, предложение — само основное сообщение
    (`_exhausted`), и второго не бывает.
    """
    admission = gate.admission
    if not (gate.offer and gate.shown and admission.withheld):
        return None
    if admission.limit != FREE_CARDS_PER_PERIOD:
        return None
    return Reply(
        wording_plan.exhausted_offer(total=None, renews=admission.period_end),
        offer_plan=True,
        passport_root=root,
    )
