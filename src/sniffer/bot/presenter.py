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

from sniffer.bot import wording
from sniffer.bot.billing import OFFER
from sniffer.bot.cards import render_cards
from sniffer.config import get_settings
from sniffer.domain.dialogue import Option, Question, feedback_buttons
from sniffer.domain.passport import Passport
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


def present(passport: Passport, found: Results, *, root: int | None) -> Reply:
    """Итог поиска одним сообщением: выдача, пустой ответ или «жду сбора».

    `root` — корень ветки: он едет в кнопках, чтобы старая выдача действовала на
    свой поиск, а не на тот, что выбран позже.
    """
    if found.items:
        return _results(passport, found, root)
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
