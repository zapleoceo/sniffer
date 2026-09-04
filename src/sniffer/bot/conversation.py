"""Ход диалога: спросить, если это сузит выдачу, иначе искать.

Здесь только последовательность шагов. Что спрашивать и что делать с ответом
решает домен (`domain/dialogue.py`), как разобрать слова — `search/answers.py`,
где хранить состояние — `bot/store.py`. Этот файл их связывает и ничего не
решает сам.

Ответы отдаются по одному, а не пачкой в конце: поиск идёт до минуты, и
«Понял, ищу» обязано прийти до выдачи, иначе бот выглядит зависшим.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, cast

import structlog

from sniffer.bot import journal
from sniffer.bot.billing import OFFER
from sniffer.bot.cards import render_cards
from sniffer.bot.store import Client, Dialogue, DialogueStore
from sniffer.broker.usage import request_scope
from sniffer.config import get_settings
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
    blocking_question,
    corrects,
    feedback_buttons,
    feedback_question,
    parse_option,
    question_by_code,
    question_for,
    restates,
)
from sniffer.domain.passport import Category, Passport
from sniffer.search.answers import interpret, is_skip
from sniffer.search.currency import usd_vnd_rate
from sniffer.search.intake import QueryIntake
from sniffer.search.intake_rules import parse_query
from sniffer.search.live import run_plan
from sniffer.search.planner import SearchPlanner
from sniffer.search.refinements import merge_edit, price_refinement
from sniffer.search.relevance import rank_items, with_vnd_budget
from sniffer.search.vocabulary import city_name, is_served, served_cities
from sniffer.sources.base import RawItem, registered_sources
from sniffer.sources.catalog_sink import remember
from sniffer.verifier import screen

log = structlog.get_logger(__name__)

NOTHING_FOUND = (
    "По этому запросу ничего не нашлось. Попробуйте иначе: без марки, "
    "с другим бюджетом или другой формулировкой."
)
# Пустая выдача — самый честный повод предложить слежение: искать больше
# негде, а новое появится.
EMPTY_WITH_OFFER = NOTHING_FOUND + "\n\n" + OFFER
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
    # Предложить слежение за новыми объявлениями. Признак, а не готовая кнопка:
    # домен решает «уместно ли», разметку рисует `keyboards`.
    offer_subscription: bool = False
    passport_root: int | None = None


class Parser(Protocol):
    """Разбор формулировки клиента. В бою — `QueryIntake`, в тестах — заглушка."""

    async def parse(self, text: str) -> Passport: ...


@dataclass(frozen=True, slots=True)
class Found:
    """Что нашлось и чем искали.

    План возвращается наружу не для красоты: дашборд показывает долю запросов,
    ушедших в фолбэк, и считает её по этому полю. Отдать одни объявления значило
    бы уронить этот столбец в тишине — а по нему видно, что брокер лежит.

    `stages` заполняет сам искатель: «запрос шёл сорок секунд» не отвечает, кто
    их съел — планировщик или источники, а от ответа зависит, что править.
    """

    items: list[RawItem]
    fallback: bool = False
    sources: tuple[str, ...] = ()
    stages: dict[str, int] = field(default_factory=dict)
    status: str | None = None


class Recorder(Protocol):
    """Куда пишется ход диалога. Реализация по умолчанию — `bot.journal`.

    Зависимость явная, а не импорт модуля внутри метода, по двум причинам.
    Первая — та же, что у разбора и поиска: подставить в тесте нечего, кроме
    подмены чужого модуля, а подмена «на всякий случай» забывается ровно в том
    тесте, где она нужна. Вторая дороже: журнал ходит в Postgres, и разговор
    без подставленного журнала тянет соединение к базе из каждого теста — сам
    журнал ошибку проглотит, но ожидание соединения останется.
    """

    async def open_request(
        self, tg_user_id: int, text: str, *, username: str | None = None
    ) -> journal.OpenRequest | None: ...

    async def log_answer(self, opened: journal.OpenRequest | None, text: str) -> None: ...

    async def close_request(
        self,
        opened: journal.OpenRequest | None,
        *,
        stages: dict[str, int],
        result_count: int = 0,
        plan_fallback: bool = False,
        sources: list[str] | None = None,
        error: str | None = None,
    ) -> None: ...


Send = Callable[[Reply], Awaitable[None]]
# Тело хода: точка входа отдаёт сюда свою работу, а обёртка `_journalled`
# берёт на себя открытие и закрытие записи. Три точки входа — одна обёртка.
Body = Callable[["Client", str, Send], Awaitable[None]]
Finder = Callable[[Passport], Awaitable["Found"]]
ScopedFinder = Callable[[Dialogue], Awaitable["Found"]]
Intake = Callable[[], Parser]


async def find_live(passport: Passport) -> Found:
    watch = journal.Stopwatch()
    sources = sorted(registered_sources())
    # План и курс независимы. Если курс не ответил, поиск всё равно идёт, но
    # цену на доске не фильтруем по выдуманной константе.
    rate_task = asyncio.create_task(usd_vnd_rate(), name="usd-vnd-rate")
    plan = await SearchPlanner().plan(passport, sources)
    rate = await rate_task
    plan = with_vnd_budget(plan, passport, rate)
    watch.lap("plan_ms")
    log.info("bot.plan", tasks=len(plan.tasks), fallback=plan.is_fallback, sources=plan.sources())
    items = rank_items(passport, await run_plan(plan), usd_vnd=rate)
    watch.lap("search_ms")
    # Последняя проверка перед показом. Стоит одну дешёвую пачку и снимает то,
    # чего детерминированные правила не видят: цену без метки «Цена» и предмет
    # не из того запроса (verifier/guard.py).
    items = await screen(passport, items, usd_vnd=rate)
    await remember(items, passport)
    watch.lap("guard_ms")
    return Found(
        items=items,
        fallback=plan.is_fallback,
        sources=tuple(plan.sources()),
        stages=watch.stages,
    )


# Текущий ход диалога. Contextvar, а не параметр, — и это лечение класса
# дефектов, а не одного случая. Раньше `_Turn` ехал аргументом через
# `_ask_or_search` и `_search`, и ЧЕТЫРЕ места из шести его теряли:
# `_answer_in_words`, `_restated`, `on_answer`, `on_feedback`. Живой след
# 01.09.2026: запрос №2 отдал клиенту пять карточек Chotot, а в журнале стоят
# `result_count = 0` и пустой `sources`; расход модели на кнопочном поиске
# приехал с `request_id = NULL`. Аргумент, который надо не забыть передать в
# шести местах, забудут в седьмом — поэтому его больше нет вовсе.
_current_turn: ContextVar[_Turn | None] = ContextVar("sniffer_dialog_turn", default=None)


@contextmanager
def _turn_scope(turn: _Turn) -> Iterator[None]:
    token = _current_turn.set(turn)
    try:
        yield
    finally:
        _current_turn.reset(token)


@dataclass(slots=True)
class _Turn:
    """Один ход диалога для журнала: сколько заняло, чем кончилось.

    Существует потому, что закрыть запись обязан тот, кто начал ход, а знает
    итог тот, кто дошёл до поиска. Передавать между ними наружу нечего —
    поэтому один объект едет по ходу и собирает факты по дороге.

    Ошибка журнала ход не роняет: недоступная база означает пустой дашборд, а
    не «бот не ответил» (см. `journal`).
    """

    recorder: Recorder
    opened: journal.OpenRequest | None
    watch: journal.Stopwatch = field(default_factory=journal.Stopwatch)
    found: Found | None = None
    failed: str | None = None

    def recording(self, send: Send) -> Send:
        """Тот же отправитель, но каждый ответ попадает и в журнал.

        Сначала человек, потом лог: сломанный журнал не должен задерживать
        ответ, а порядок «сначала записать» ровно это и делал бы.
        """

        async def recorded(reply: Reply) -> None:
            await send(reply)
            await self.recorder.log_answer(self.opened, reply.text)

        return recorded

    async def close(self, *, error: str | None = None) -> None:
        reason = error or self.failed
        await self.recorder.close_request(
            self.opened,
            stages={**self.watch.stages, **(self.found.stages if self.found else {})},
            result_count=len(self.found.items) if self.found else 0,
            plan_fallback=bool(self.found and self.found.fallback),
            sources=list(self.found.sources) if self.found else [],
            error=reason,
        )


class Conversation:
    def __init__(
        self,
        store: DialogueStore,
        *,
        intake: Intake = QueryIntake,
        finder: Finder = find_live,
        scoped_finder: ScopedFinder | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self._store = store
        self._intake = intake
        self._finder = finder
        self._scoped_finder = scoped_finder
        # По умолчанию — настоящий журнал: разговор создаётся хендлером без
        # аргументов, и всё, что не подставлено здесь, на боевом пути не
        # появится никогда.
        # `cast`, потому что модуль под протокол подходит по существу, но не по
        # типу: mypy видит `Module`, а не класс с тремя методами. Проверяет
        # соответствие тест `test_the_real_journal_fits_the_recorder_protocol` —
        # иначе расхождение вылезло бы в проде, где подставлен именно модуль.
        self._recorder: Recorder = recorder or cast(Recorder, journal)

    async def on_text(self, client: Client, text: str, send: Send) -> None:
        message = text.strip()
        if not message:
            return
        await self._journalled(client, message, send, self._turn)

    async def _journalled(self, client: Client, query: str, send: Send, body: Body) -> None:
        """Один ход диалога целиком: запись открыта, закрыта и не потеряна.

        Журнал живёт здесь, а не в хендлере: границу единицы работы ставит тот,
        кто владеет ходом, а хендлер о разборе, вопросах и поиске не знает
        ничего (architecture.md, 5.1). И здесь же — для ВСЕХ трёх точек входа,
        а не только для текстовой: нажатие кнопки запускает такой же поиск и
        стоит таких же денег.
        """
        opened = await self._recorder.open_request(
            client.tg_user_id, query, username=client.username
        )
        turn = _Turn(recorder=self._recorder, opened=opened)
        # Расходы на модель принадлежат ЭТОМУ запросу: contextvar доносит его id
        # до клиента брокера через слои, которым он не нужен, и не путается
        # между двумя клиентами, отвечающими одновременно.
        with request_scope(opened.request_id if opened else None), _turn_scope(turn):
            try:
                await body(client, query, turn.recording(send))
            except Exception as exc:
                # Ход обязан закрыться в журнале даже сломанным: иначе в
                # дашборде видны только удачные запросы, то есть картина ровно
                # обратная той, ради которой журнал заведён.
                await turn.close(error=f"{type(exc).__name__}: {exc}")
                raise
            await turn.close()

    async def _turn(self, client: Client, message: str, send: Send) -> None:
        dialogue = await self._store.load(client)
        current = dialogue.passport
        if current is not None and (not dialogue.state.pending or dialogue.editing):
            refined = price_refinement(current.passport, message)
            if refined is not None:
                dialogue = await self._store.revise(
                    dialogue,
                    refined,
                    kind=EVENT_MANUAL_EDIT,
                    payload={"field": "budget.max", "text": message},
                )
                await self._ask_or_search(dialogue, send)
                return
        if dialogue.editing and current is not None:
            passport = await self._intake().parse(message)
            passport = merge_edit(current.passport, passport)
            _lap("intake_ms")
            dialogue = await self._store.revise(
                dialogue,
                passport,
                kind=EVENT_MANUAL_EDIT,
                payload={"field": "query", "value": message},
            )
            await self._ask_or_search(dialogue, send)
            return
        if dialogue.state.pending and current is not None:
            answered = await self._answer_in_words(dialogue, dialogue.state.pending, message, send)
            if answered:
                return

        passport = await self._intake().parse(message)
        _lap("intake_ms")
        if current is not None and restates(current.passport, passport):
            # Та же просьба другими словами — не новый запрос. Начни здесь
            # цепочка заново, и повтор фразы обнулял бы собранные ответы вместе
            # со счётчиком вопросов: лимит обходился бы копипастом.
            await self._restated(dialogue, send)
            return
        if current is not None and corrects(message):
            # Поправка — уточнение ТОЙ ЖЕ просьбы: новая версия в той же
            # цепочке, а не новый запрос. Живой след 03.09.2026: «найди мне
            # моцокил 200 кубиков» → бот прочёл 200 000 VND бюджетом → «не
            # 200000 VND, а обьем … до 200 кубических сантиметров» → цепочка
            # начиналась заново, категория из первого сообщения исчезала, и бот
            # спрашивал «Что ищем?» у человека, который только что объяснил,
            # что именно поняли не так.
            #
            # `restates` этого не спасал и не мог: поправка приносит
            # содержательные слова («кубических», «сантиметров»), то есть по
            # словам она новый запрос. Отличает её противопоставление «не X, а
            # Y», и решает это `corrects`; слияние фактов делает `merge_edit` —
            # тот же, что у ответа словами, чтобы знание было одно.
            dialogue = await self._store.revise(
                dialogue,
                merge_edit(current.passport, passport),
                kind=EVENT_USER_MESSAGE,
                payload={"correction": message},
            )
            await self._ask_or_search(dialogue, send)
            return
        dialogue = await self._store.start(dialogue, passport)
        await self._ask_or_search(dialogue, send)

    async def repeat(self, client: Client, root: int, send: Send) -> None:
        """Повторить выбранный запрос без нового разбора и новой версии."""
        await self._journalled(
            client,
            f"повтор запроса {root}",
            send,
            lambda _client, _query, recorded: self._repeat(client, root, recorded),
        )

    async def _repeat(self, client: Client, root: int, send: Send) -> None:
        dialogue = await self._store.load(client)
        dialogue = await self._store.select(dialogue, root)
        if dialogue.passport is None or dialogue.passport.root != root:
            await send(Reply(NO_REQUEST_YET))
            return
        await self._ask_or_search(dialogue, send)

    async def on_answer(self, client: Client, code: str, value: str, send: Send) -> None:
        """Клиент нажал кнопку под вопросом. Ход журналируется как текстовый.

        Раньше здесь записи не открывалось вовсе: карточки уходили клиенту, а в
        дашборде запроса не было, и расход модели приезжал с `request_id = NULL`.
        Нажатие кнопки запускает такой же поиск и стоит таких же денег.
        """
        await self._journalled(
            client,
            f"кнопка: {code}={value}",
            send,
            lambda _client, _query, recorded: self._answered(client, code, value, recorded),
        )

    async def _answered(self, client: Client, code: str, value: str, send: Send) -> None:
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
            if not question.skippable:
                await self._ask(dialogue, question, send)
                return
            dialogue = await self._skip(dialogue, question.field)
        else:
            base = current.passport
            if question.field == "category":
                base = merge_edit(base, parse_query(value))
            passport = apply_answer(base, question.field, parse_option(question.field, value))
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
        создаёт новую версию паспорта и перезапускает подбор. И журналируется
        как отдельный ход: это полноценный поиск, а не довесок к прошлому.
        """
        await self._journalled(
            client,
            f"кнопка: {kind.value}",
            send,
            lambda _client, _query, recorded: self._feedback(client, kind, recorded),
        )

    async def _feedback(self, client: Client, kind: Feedback, send: Send) -> None:
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
        await self._ask_or_search(dialogue, send)

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
            fresh = parse_query(text)
            if fresh.category not in (None, current.passport.category) and pending != "category":
                return False
            passport = merge_edit(current.passport, fresh)
            passport = apply_answer(passport, pending, value)
            dialogue = await self._store.revise(
                dialogue,
                passport,
                kind=EVENT_USER_MESSAGE,
                payload={"field": pending, "text": text},
            )
        elif is_skip(text):
            question = question_for(pending)
            if question is not None and not question.skippable:
                await self._ask(dialogue, question, send)
                return True
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
        category_question = blocking_question(passport, dialogue.state.asked)
        if category_question is not None and category_question.field == "category":
            await self._ask(dialogue, category_question, send)
            return
        if not is_served(passport.city):
            # Искать в городе, под который не собран ни реестр чатов, ни
            # параметры досок, нечем. Сказать это прямо — единственный честный
            # ответ: уточнять бюджет в городе, где нет источников, значит
            # тратить вопросы клиента впустую.
            await send(Reply(_unserved(passport.city)))
            return
        question = blocking_question(passport, dialogue.state.asked)
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
        await send(
            Reply(
                question.text,
                question=question,
                passport_root=dialogue.passport.root if dialogue.passport else None,
            )
        )

    async def _search(self, dialogue: Dialogue, send: Send) -> None:
        if dialogue.passport is None:  # pragma: no cover — сюда приходят с паспортом
            return
        passport = dialogue.passport.passport
        await send(Reply(_accepted(passport)))

        turn = _current_turn.get()
        try:
            found = (
                await self._scoped_finder(dialogue)
                if self._scoped_finder is not None
                else await self._finder(passport)
            )
        except Exception as exc:
            # Граница запроса: неожиданная ошибка внутри поиска не должна
            # оставлять клиента без ответа. Трейсбек уходит в лог целиком.
            log.exception("bot.search_failed", passport_id=dialogue.passport.id)
            await send(Reply(SEARCH_FAILED))
            if turn is not None:
                turn.failed = f"{type(exc).__name__}: {exc}"
            return

        if turn is not None:
            turn.found = found
        if not found.items:
            # Пустая выдача — самый честный повод предложить слежение: искать
            # больше негде, а новое появится.
            await send(
                Reply(
                    f"{found.status}\n\n{OFFER}" if found.status else EMPTY_WITH_OFFER,
                    offer_subscription=True,
                    passport_root=dialogue.passport.root,
                )
            )
            return
        shown = min(len(found.items), get_settings().max_cards)
        header = _result_header(passport, len(found.items), shown)
        if found.status:
            header = f"{found.status}\n\n{header}"
        await send(
            Reply(
                f"{header}\n\n{render_cards(found.items)}",
                feedback=feedback_buttons(passport),
                offer_subscription=True,
                passport_root=dialogue.passport.root,
            )
        )


# Как назвать категорию во множественном числе в заголовке выдачи. Русские слова,
# потому что заголовок читает человек, а не `passport.category.value` («motorbike»).
_CATEGORY_PLURAL: dict[Category, str] = {
    Category.MOTORBIKE: "байков",
    Category.APARTMENT: "квартир",
    Category.ROOM: "комнат",
    Category.HOUSE: "домов",
    Category.CAR: "машин",
    Category.BICYCLE: "велосипедов",
}


def _is_broad(passport: Passport) -> bool:
    """Запрос без единого сужающего факта — только категория (и, может, город).

    «Скутер» без бюджета, марки, модели и объёма — это ещё не запрос, а тема:
    под неё подходит пол-базы. Показать пять свежих и молча назвать это ответом —
    ровно та жалоба владельца «находит шлак в большом количестве, не уточняя».
    Такой выдаче нужен честный заголовок и приглашение сузить.
    """
    a = passport.attributes
    return not (a.get("model") or a.get("brand") or a.get("engine_cc") or passport.budget.max)


def _result_header(passport: Passport, total: int, shown: int) -> str:
    """Строка над карточками: что нашлось и, если запрос широкий, как сузить.

    Объяснение — не вежливость, а ответ на «не объясняя»: пять карточек без
    контекста не говорят, из скольких они выбраны и почему именно эти. Широкий
    запрос вдобавок сам просит сузить — но не вопросом до выдачи (это была бы
    прежняя форма, которую владелец отверг), а приглашением поверх уже показанных
    результатов: search-first остаётся.
    """
    if total <= shown:
        return "Вот что нашлось:" if total > 1 else "Нашёлся один вариант:"
    if _is_broad(passport):
        noun = _CATEGORY_PLURAL.get(passport.category) if passport.category else None
        many = f"{noun} нашлось много" if noun else "нашлось много"
        return (
            f"Запрос широкий — {many} ({total}). Показываю {shown} самых свежих.\n"
            "Чтобы сузить, допишите бюджет, марку или модель — например «yamaha до 500» "
            "или «honda lead»."
        )
    return f"Нашёл {total}, показываю {shown} самых подходящих:"


def _lap(stage: str) -> None:
    """Отметить этап у текущего хода, если он есть."""
    turn = _current_turn.get()
    if turn is not None:
        turn.watch.lap(stage)


def _unserved(city: str | None) -> str:
    """Список городов берётся из словаря: набранный руками, он разъедется первым."""
    return UNSERVED_CITY.format(
        city=city_name(city, "ru") or "Этот город", served=", ".join(served_cities("ru"))
    )


def _accepted(passport: Passport) -> str:
    """Показываем, что поняли, — это дешевле лишнего уточняющего вопроса."""
    parts: list[str] = []
    if passport.category:
        category = (
            "скутер"
            if passport.attributes.get("body_type") == "tay_ga"
            else {
                Category.MOTORBIKE: "мотобайк",
                Category.APARTMENT: "квартира",
                Category.ROOM: "комната",
                Category.HOUSE: "дом",
                Category.BICYCLE: "велосипед",
                Category.CAR: "автомобиль",
            }.get(passport.category, passport.category.value)
        )
        parts.append(category)
    for key in ("brand", "model"):
        if passport.attributes.get(key):
            parts.append(str(passport.attributes[key]))
    city = city_name(passport.city, "ru")
    if city:
        parts.append(city)
    if passport.budget.max:
        currency = passport.budget.currency.value if passport.budget.currency else ""
        amount = f"{passport.budget.max:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
        parts.append(f"до {amount} {currency}".strip())
    transmission = passport.attributes.get("transmission")
    if transmission:
        parts.append(
            {"automatic": "автомат", "manual": "механика", "semi": "полуавтомат"}.get(
                str(transmission), str(transmission)
            )
        )
    understood = ", ".join(parts) if parts else "запрос как есть"
    return f"Понял: {understood}. Ищу, это занимает до минуты."
