"""Ход диалога: спросить, если это сузит выдачу, иначе искать.

Здесь только последовательность шагов. Что спрашивать и что делать с ответом
решает домен (`domain/dialogue.py`), как разобрать слова — `search/answers.py`,
где хранить состояние — `bot/store.py`. Этот файл их связывает и ничего не
решает сам.

Ответы отдаются по одному, а не пачкой в конце: поиск идёт до минуты, и
«Понял, ищу» обязано прийти до выдачи, иначе бот выглядит зависшим.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from sniffer.bot.cards import render_cards
from sniffer.bot.store import Client, Dialogue, DialogueStore
from sniffer.domain.dialogue import (
    EVENT_FEEDBACK,
    EVENT_MANUAL_EDIT,
    EVENT_QUESTION_ASKED,
    EVENT_USER_MESSAGE,
    SKIP,
    Feedback,
    Option,
    Question,
    apply_answer,
    apply_feedback,
    feedback_buttons,
    feedback_question,
    next_question,
    parse_option,
    question_by_code,
    question_for,
    restates,
)
from sniffer.domain.passport import Passport
from sniffer.search.answers import interpret, is_skip
from sniffer.search.intake import QueryIntake
from sniffer.search.live import run_plan
from sniffer.search.planner import SearchPlanner
from sniffer.search.vocabulary import city_name, is_served, served_cities
from sniffer.sources.base import RawItem, registered_sources

log = structlog.get_logger(__name__)

NOTHING_FOUND = (
    "По этому запросу ничего не нашлось. Попробуйте иначе: без марки, "
    "с другим бюджетом или другой формулировкой."
)
SEARCH_FAILED = "Не смог доискать: источники не ответили. Попробуйте ещё раз через пару минут."
NO_REQUEST_YET = "Сначала напишите, что ищете, — а потом уточним."
NOTHING_TO_REFINE = "Уточнить больше нечего. Переформулируйте запрос, и поищу заново."
UNSERVED_CITY = (
    "{city} я пока не ищу: реестр чатов и параметры досок собраны под другие города — "
    "{served}. Напишите запрос по одному из них, и поищу."
)


@dataclass(frozen=True, slots=True)
class Reply:
    """Одно сообщение клиенту. Кнопки описаны доменом, рисует их `keyboards`."""

    text: str
    question: Question | None = None
    feedback: tuple[Option, ...] = field(default_factory=tuple)


class Parser(Protocol):
    """Разбор формулировки клиента. В бою — `QueryIntake`, в тестах — заглушка."""

    async def parse(self, text: str) -> Passport: ...


Send = Callable[[Reply], Awaitable[None]]
Finder = Callable[[Passport], Awaitable[list[RawItem]]]
Intake = Callable[[], Parser]


async def find_live(passport: Passport) -> list[RawItem]:
    sources = sorted(registered_sources())
    plan = await SearchPlanner().plan(passport, sources)
    log.info("bot.plan", tasks=len(plan.tasks), fallback=plan.is_fallback, sources=plan.sources())
    return await run_plan(plan)


class Conversation:
    def __init__(
        self,
        store: DialogueStore,
        *,
        intake: Intake = QueryIntake,
        finder: Finder = find_live,
    ) -> None:
        self._store = store
        self._intake = intake
        self._finder = finder

    async def on_text(self, client: Client, text: str, send: Send) -> None:
        message = text.strip()
        if not message:
            return

        dialogue = await self._store.load(client)
        current = dialogue.passport
        if dialogue.state.pending and current is not None:
            answered = await self._answer_in_words(dialogue, dialogue.state.pending, message, send)
            if answered:
                return

        passport = await self._intake().parse(message)
        if current is not None and restates(current.passport, passport):
            # Та же просьба другими словами — не новый запрос. Начни здесь
            # цепочка заново, и повтор фразы обнулял бы собранные ответы вместе
            # со счётчиком вопросов: лимит обходился бы копипастом.
            await self._restated(dialogue, send)
            return
        dialogue = await self._store.start(dialogue, passport)
        await self._ask_or_search(dialogue, send)

    async def on_answer(self, client: Client, code: str, value: str, send: Send) -> None:
        """Клиент нажал кнопку под вопросом."""
        dialogue = await self._store.load(client)
        current = dialogue.passport
        question = question_by_code(code)
        if question is None or current is None:
            await send(Reply(NO_REQUEST_YET))
            return
        if dialogue.state.pending != question.field:
            # Кнопка старого вопроса: клавиатура остаётся в чате и после
            # ответа, а второе нажатие не должно плодить версии паспорта.
            return

        if value == SKIP:
            dialogue = await self._skip(dialogue, question.field)
        else:
            passport = apply_answer(
                current.passport, question.field, parse_option(question.field, value)
            )
            dialogue = await self._store.revise(
                dialogue,
                passport,
                kind=EVENT_MANUAL_EDIT,
                payload={"field": question.field, "value": value},
            )
        await self._ask_or_search(dialogue, send)

    async def on_feedback(self, client: Client, kind: Feedback, send: Send) -> None:
        """Кнопка под выдачей: «дорого», «не то», «нужен автомат».

        Показанная выдача уточняет запрос лучше вопроса — поэтому нажатие
        создаёт новую версию паспорта и перезапускает подбор.
        """
        dialogue = await self._store.load(client)
        if dialogue.passport is None:
            await send(Reply(NO_REQUEST_YET))
            return

        passport = apply_feedback(dialogue.passport.passport, kind)
        if passport is None:
            await self._ask_after_feedback(dialogue, kind, send)
            return

        dialogue = await self._store.revise(
            dialogue, passport, kind=EVENT_FEEDBACK, payload={"feedback": kind.value}
        )
        await self._search(dialogue, send)

    async def _answer_in_words(
        self, dialogue: Dialogue, pending: str, text: str, send: Send
    ) -> bool:
        """Ответ словами вместо кнопки. `False` — это не ответ, а новый запрос."""
        current = dialogue.passport
        if current is None:  # pragma: no cover — проверено вызывающим
            return False

        # Разбор поля идёт раньше проверки «не важно», а не наоборот: подпись
        # кнопки «любой, лишь бы ездил» иначе читалась бы как пропуск, хотя
        # сама кнопка ставит `worn`. Слово и кнопка обязаны означать одно.
        value = interpret(pending, text)
        if value is not None:
            passport = apply_answer(current.passport, pending, value)
            dialogue = await self._store.revise(
                dialogue,
                passport,
                kind=EVENT_USER_MESSAGE,
                payload={"field": pending, "text": text},
            )
        elif is_skip(text):
            dialogue = await self._skip(dialogue, pending)
        else:
            return False
        await self._ask_or_search(dialogue, send)
        return True

    async def _restated(self, dialogue: Dialogue, send: Send) -> None:
        """Повтор той же просьбы: висящий вопрос повторяем, иначе ищем заново.

        Спросить следующее поле было бы тем же обходом лимита, только на один
        вопрос дешевле, а промолчать — оставить клиента без вопроса, ответа на
        который бот ждёт. Повторный `question_asked` счётчик не двигает:
        `advance` не дублирует уже заданное поле.
        """
        pending = question_for(dialogue.state.pending or "")
        if pending is not None:
            await self._ask(dialogue, pending, send)
            return
        await self._ask_or_search(dialogue, send)

    async def _skip(self, dialogue: Dialogue, field_name: str) -> Dialogue:
        """«Не важно» не меняет паспорт — значит, и версии не создаёт."""
        return await self._store.note(
            dialogue,
            kind=EVENT_MANUAL_EDIT,
            payload={"field": field_name, "skipped": True},
        )

    async def _ask_or_search(self, dialogue: Dialogue, send: Send) -> None:
        if dialogue.passport is None:  # pragma: no cover — сюда приходят с паспортом
            return
        passport = dialogue.passport.passport
        if not is_served(passport.city):
            # Искать в городе, под который не собран ни реестр чатов, ни
            # параметры досок, нечем. Сказать это прямо — единственный честный
            # ответ: уточнять бюджет в городе, где нет источников, значит
            # тратить вопросы клиента впустую.
            await send(Reply(_unserved(passport.city)))
            return
        question = next_question(passport, dialogue.state.asked)
        if question is None:
            await self._search(dialogue, send)
            return
        await self._ask(dialogue, question, send)

    async def _ask_after_feedback(self, dialogue: Dialogue, kind: Feedback, send: Send) -> None:
        if dialogue.passport is None:  # pragma: no cover — проверено вызывающим
            return
        question = feedback_question(dialogue.passport.passport, kind, dialogue.state.asked)
        if question is None:
            await send(Reply(NOTHING_TO_REFINE))
            return
        await self._ask(dialogue, question, send)

    async def _ask(self, dialogue: Dialogue, question: Question, send: Send) -> None:
        await self._store.note(
            dialogue, kind=EVENT_QUESTION_ASKED, payload={"field": question.field}
        )
        await send(Reply(question.text, question=question))

    async def _search(self, dialogue: Dialogue, send: Send) -> None:
        if dialogue.passport is None:  # pragma: no cover — сюда приходят с паспортом
            return
        passport = dialogue.passport.passport
        await send(Reply(_accepted(passport)))

        try:
            items = await self._finder(passport)
        except Exception:
            # Граница запроса: неожиданная ошибка внутри поиска не должна
            # оставлять клиента без ответа. Трейсбек уходит в лог целиком.
            log.exception("bot.search_failed", passport_id=dialogue.passport.id)
            await send(Reply(SEARCH_FAILED))
            return

        if not items:
            await send(Reply(NOTHING_FOUND))
            return
        await send(Reply(render_cards(items), feedback=feedback_buttons(passport)))


def _unserved(city: str | None) -> str:
    """Список городов берётся из словаря: набранный руками, он разъедется первым."""
    return UNSERVED_CITY.format(
        city=city_name(city, "ru") or "Этот город", served=", ".join(served_cities("ru"))
    )


def _accepted(passport: Passport) -> str:
    """Показываем, что поняли, — это дешевле лишнего уточняющего вопроса."""
    parts: list[str] = []
    if passport.category:
        parts.append(passport.category.value)
    city = city_name(passport.city, "ru")
    if city:
        parts.append(city)
    if passport.budget.max:
        currency = passport.budget.currency.value if passport.budget.currency else ""
        parts.append(f"до {passport.budget.max:g} {currency}".strip())
    transmission = passport.attributes.get("transmission")
    if transmission:
        parts.append(str(transmission))
    understood = ", ".join(parts) if parts else "запрос как есть"
    return f"Понял: {understood}. Ищу, это занимает до минуты."
