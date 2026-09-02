"""Postgres и журнал — подделками. Всё остальное в симуляции настоящее.

Хранилище считает версии паспорта по-настоящему: из цепочки версий и событий
собирается `DialogueState`, то есть счётчик заданных вопросов и висящий вопрос.
Упрости здесь — и метрика «сколько вопросов до выдачи» начала бы мерить
подделку, а не бота.

Тот же по устройству словарь лежит в `tests/test_bot_dialog.py::MemoryStore`.
Это одно знание в двух копиях, и правильный конец у него один: тот файл
импортирует отсюда. Пока копии две, менять их надо парой — иначе тесты диалога
и симулятор разойдутся в понимании того, что такое «версия паспорта».
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sniffer.bot import journal
from sniffer.bot.store import Client, Dialogue
from sniffer.domain.dialogue import EVENT_USER_MESSAGE, DialogueState, advance, replay
from sniffer.domain.passport import Passport
from sniffer.domain.records import PassportEvent, StoredPassport


class MemoryStore:
    """`DialogueStore` на списках: диалог без базы, но с настоящими версиями."""

    def __init__(self) -> None:
        self.rows: list[StoredPassport] = []
        self.events: list[PassportEvent] = []
        self._users: dict[int, int] = {}

    async def load(self, client: Client) -> Dialogue:
        user_id = self._users.setdefault(client.tg_user_id, len(self._users) + 1)
        current = next(
            (row for row in reversed(self.rows) if row.user_id == user_id and row.is_current),
            None,
        )
        if current is None:
            return Dialogue(user_id=user_id)
        root = current.root
        chain = {row.id for row in self.rows if row.id == root or row.root_id == root}
        events = [event for event in self.events if event.passport_id in chain]
        return Dialogue(user_id=user_id, passport=current, state=replay(events))

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue:
        stored = StoredPassport(
            id=len(self.rows) + 1, user_id=dialogue.user_id, version=1, passport=passport
        )
        self.rows.append(stored)
        self._event(stored.id, EVENT_USER_MESSAGE, {"text": passport.raw_query})
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue:
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("нечего уточнять: паспорта ещё нет")
        root = dialogue.passport.root
        self.rows = [
            replace(row, is_current=False) if row.id == root or row.root_id == root else row
            for row in self.rows
        ]
        stored = StoredPassport(
            id=len(self.rows) + 1,
            user_id=dialogue.user_id,
            version=dialogue.passport.version + 1,
            root_id=root,
            passport=passport,
        )
        self.rows.append(stored)
        self._event(stored.id, kind, payload)
        return Dialogue(
            user_id=dialogue.user_id,
            passport=stored,
            state=advance(dialogue.state, kind, payload),
        )

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue:
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("событие без паспорта")
        self._event(dialogue.passport.id, kind, payload)
        return replace(dialogue, state=advance(dialogue.state, kind, payload))

    def _event(self, passport_id: int, kind: str, payload: dict[str, Any]) -> None:
        self.events.append(PassportEvent(passport_id=passport_id, kind=kind, payload=payload))


class SilentJournal:
    """`Recorder`, который никуда не пишет.

    Подставляется ВСЕГДА, а не по случаю: без него разговор берёт настоящий
    журнал, тот идёт в Postgres, и отчёт либо ждёт таймаут соединения, либо
    зависит от того, поднята ли рядом база. Симулятор обязан работать на
    ноутбуке без Docker так же, как в CI.
    """

    async def open_request(
        self, tg_user_id: int, text: str, *, username: str | None = None
    ) -> journal.OpenRequest | None:
        return None

    async def log_answer(self, opened: journal.OpenRequest | None, text: str) -> None:
        return None

    async def close_request(
        self,
        opened: journal.OpenRequest | None,
        *,
        stages: dict[str, int],
        result_count: int = 0,
        plan_fallback: bool = False,
        sources: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        return None
