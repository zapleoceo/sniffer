"""Postgres и журнал — подделками. Всё остальное в симуляции настоящее.

Хранилище считает версии паспорта по-настоящему: из цепочки версий и событий
собирается `DialogueState`, то есть счётчик заданных вопросов и висящий вопрос.
Упрости здесь — и метрика «сколько вопросов до выдачи» начала бы мерить
подделку, а не бота.

Подделка одна на тесты диалога и симулятор: тесты импортируют её отсюда. Две
копии разошлись бы в понимании того, что такое «версия паспорта» и что снимает
взведённое `/new`, — и тесты показывали бы одно поведение, а симулятор другое.
Что именно подделка обязана делать так же, как `PassportStore`, закреплено
контрактными тестами (`tests/test_store_contract.py`): они идут на обеих.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sniffer.bot import journal
from sniffer.bot.store import Client, Dialogue
from sniffer.domain.dialogue import EVENT_USER_MESSAGE, DialogueState, advance, replay
from sniffer.domain.passport import Passport
from sniffer.domain.records import PassportEvent, QueryOverview, StoredPassport
from sniffer.domain.threads import MAX_LIVE_THREADS


class MemoryStore:
    """`DialogueStore` на списках: диалог без базы, но с настоящими версиями."""

    def __init__(self) -> None:
        self.rows: list[StoredPassport] = []
        self.events: list[PassportEvent] = []
        # Состояние мониторинга по корню ветки (`active` / `paused` / `expired`).
        # Подписок в подделке нет, а текст о вытеснении зависит от них, поэтому
        # тест выставляет состояние руками; не названная ветка — без мониторинга.
        self.monitoring: dict[int, str] = {}
        self._users: dict[int, int] = {}
        self._active: dict[int, int] = {}
        self._editing: set[int] = set()
        self._awaiting: set[int] = set()
        # Когда ветку использовали в последний раз: порядок списка, как
        # `passports.last_used_at` в базе. Счётчик, а не время: тест не ждёт часов.
        self._used: dict[int, int] = {}
        self._clock = 0

    async def load(self, client: Client) -> Dialogue:
        return self._dialogue(self._users.setdefault(client.tg_user_id, len(self._users) + 1))

    def _dialogue(self, user_id: int) -> Dialogue:
        """Разговор клиента по его id: так же читает его `load` и возвращает `select`."""
        active = self._active.get(user_id)
        current = next(
            (
                row
                for row in reversed(self.rows)
                if row.user_id == user_id
                and row.is_current
                and (active is None or row.root == active)
            ),
            None,
        )
        if current is None:
            return Dialogue(user_id=user_id)
        root = current.root
        chain = {row.id for row in self.rows if row.id == root or row.root_id == root}
        events = [event for event in self.events if event.passport_id in chain]
        return Dialogue(
            user_id=user_id,
            passport=current,
            state=replay(events),
            editing=current.root in self._editing,
            starting_new=user_id in self._awaiting,
        )

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue:
        stored = StoredPassport(
            id=len(self.rows) + 1, user_id=dialogue.user_id, version=1, passport=passport
        )
        self.rows.append(stored)
        self._active[dialogue.user_id] = stored.root
        self._editing.discard(stored.root)
        self._awaiting.discard(dialogue.user_id)
        self._touch(stored.root)
        self._event(stored.id, EVENT_USER_MESSAGE, {"text": passport.raw_query})
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def start_requested(self, dialogue: Dialogue, passport: Passport) -> Dialogue | None:
        """Как в базе: создаёт, только если флаг ещё взведён. Нет «await» — нет и гонки."""
        if dialogue.user_id not in self._awaiting:
            return None
        return await self.start(dialogue, passport)

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
        self._active[dialogue.user_id] = root
        self._editing.discard(root)
        # Правка — тоже выбор ветки: в базе её делает `save_revision` через
        # `select`, и флаг `/new` вместе с ним. Подделка, которая этого не делает,
        # показывала бы тестам одно поведение, а проду другое.
        self._awaiting.discard(dialogue.user_id)
        self._touch(root)
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

    async def live_threads(self, dialogue: Dialogue) -> list[QueryOverview]:
        """Ветки в списке: недавно использованные сверху, не больше предела — как в SQL.

        Предел повторён здесь не ради красоты: по длине этого списка бот решает,
        вытесняется ли ветка, и список без предела никогда не сказал бы «мест нет».
        Порядок — по использованию, а не по правке: выбор вытесненной ветки
        возвращает её в список, как в базе.
        """
        active = self._active.get(dialogue.user_id)
        mine = [row for row in self.rows if row.user_id == dialogue.user_id and row.is_current]
        mine.sort(key=lambda row: (self._used.get(row.root, 0), row.id), reverse=True)
        return [
            QueryOverview(
                root=row.root,
                passport=row.passport,
                is_active=row.root == active,
                monitoring=self.monitoring.get(row.root, "off"),
            )
            for row in mine[:MAX_LIVE_THREADS]
        ]

    async def await_new(self, dialogue: Dialogue) -> None:
        self._awaiting.add(dialogue.user_id)

    async def select(self, dialogue: Dialogue, root: int, *, editing: bool = False) -> Dialogue:
        owned = any(row.user_id == dialogue.user_id and row.root == root for row in self.rows)
        if not owned:
            return dialogue
        self._active[dialogue.user_id] = root
        self._awaiting.discard(dialogue.user_id)
        self._touch(root)
        if editing:
            self._editing.add(root)
        else:
            self._editing.discard(root)
        return self._dialogue(dialogue.user_id)

    def _touch(self, root: int) -> None:
        self._clock += 1
        self._used[root] = self._clock

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
