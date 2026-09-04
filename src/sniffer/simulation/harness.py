"""Прогон сценария через настоящий `Conversation` и сбор фактов о ходе.

Здесь только НАБЛЮДЕНИЕ. Ни одного суждения о том, хорошо это или плохо, —
судит `verdict.py`. Разделение не косметическое: факты снимаются через
публичный интерфейс разговора (`on_text`, `on_answer`, `on_feedback`, `Reply`),
и пока он не меняется, харнес переживает любую перестройку `search/` и
`domain/`. Загляни он внутрь — правка соседнего модуля красила бы его в
красный, ничего не сообщая о качестве диалога.

Показанные карточки узнаются по ссылкам в тексте ответа, а не по тому, что
вернул искатель: между ними стоит `render_cards`, который режет выдачу до
`MAX_CARDS`. Клиент видит именно ссылки — их и считаем.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sniffer.bot.conversation import Conversation, Reply
from sniffer.bot.store import Client
from sniffer.config import get_settings
from sniffer.domain.passport import Passport
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.catalog import CATALOG, Lot
from sniffer.simulation.fit import off_target
from sniffer.simulation.market import lots_by_url, market_finder
from sniffer.simulation.scenarios import SCENARIOS
from sniffer.simulation.script import Says, Scenario, Step, Taps
from sniffer.simulation.stubs import MemoryStore, SilentJournal
from sniffer.verifier.liveness import assess

CLIENT = Client(tg_user_id=4242, username="owner")

_HREF_RE = re.compile(r'href="([^"]+)"')


class _RulesIntake:
    """Разбор без модели — тот самый путь, по которому бот идёт, когда брокер молчит.

    Подставлять сюда заранее заготовленный паспорт нельзя принципиально: тогда
    метрика «что распозналось» мерила бы нашу заготовку. Правила настоящие, и
    «200 кубиков» они разбирают ровно так же, как в бою.
    """

    async def parse(self, text: str) -> Passport:
        # `default_city`, как в бою (`intake.QueryIntake.parse`): город
        # подставляется, а не спрашивается (search-first). Без него симуляция
        # расходилась с продом — город в паспорте появлялся, только если назван
        # словом, и метрика диалога врала бы про лишний вопрос о городе.
        return parse_query(text, default_city=get_settings().default_city)


@dataclass(frozen=True, slots=True)
class Shown:
    """Одна показанная карточка и что с ней не так по правде о лоте."""

    external_id: str
    reason: str
    stale: bool


@dataclass(frozen=True, slots=True)
class Metrics:
    """Факты об одном прогоне. Ни одной оценки — только числа и тексты."""

    scenario: Scenario
    # Главная цифра: сколько вопросов клиент выслушал ДО первой карточки.
    # Только недостающие категория, город и бюджет: от нуля до трёх.
    questions_before_results: int
    # Вопросы всего: обратная связь вправе спросить и после выдачи, и это
    # нормально. Разница между этим числом и предыдущим — цена уточнений.
    questions_total: int
    asked_fields: tuple[str, ...]
    repeated_questions: tuple[str, ...]
    # Сколько сообщений клиенту пришлось написать до первой выдачи. Отдельно от
    # числа вопросов: бот, который «не спросил, но и не помог», по вопросам
    # выглядит идеально.
    client_turns_to_results: int | None
    reached_results: bool
    shown: tuple[Shown, ...]
    found_nothing: bool
    # Ответы-заглушки («сначала напишите, что ищете», «уточнить больше нечего»,
    # «не смог доискать»): бот ответил, но помощи в этом ноль.
    stub_replies: tuple[str, ...]
    silent_steps: tuple[int, ...]
    passport_fields: dict[str, object] = field(default_factory=dict)
    replies: tuple[str, ...] = ()

    @property
    def cards_shown(self) -> int:
        return len(self.shown)

    @property
    def off_target(self) -> tuple[Shown, ...]:
        return tuple(card for card in self.shown if card.reason)

    @property
    def stale_shown(self) -> int:
        return sum(1 for card in self.shown if card.stale)


async def run_scenario(scenario: Scenario, *, catalog: tuple[Lot, ...] = CATALOG) -> Metrics:
    """Проиграть сценарий и снять с него метрики."""
    store = MemoryStore()
    talker = Conversation(
        store, intake=_RulesIntake, finder=market_finder, recorder=SilentJournal()
    )
    index = lots_by_url(catalog)

    sent: list[Reply] = []
    shown: list[Shown] = []
    silent: list[int] = []
    turns_to_results: int | None = None

    for number, step in enumerate(scenario.steps, start=1):
        seen = len(sent)
        await _play(talker, step, sent)
        fresh = sent[seen:]
        if not fresh:
            silent.append(number)
        passport = _current(store)
        cards = [card for reply in fresh for card in _cards(reply.text, index, passport)]
        shown += cards
        if cards and turns_to_results is None:
            turns_to_results = number

    questions = [reply.question.field for reply in sent if reply.question is not None]
    return Metrics(
        scenario=scenario,
        questions_before_results=_questions_before_cards(sent),
        questions_total=len(questions),
        asked_fields=tuple(questions),
        repeated_questions=tuple(
            sorted({field for field in questions if questions.count(field) > 1})
        ),
        client_turns_to_results=turns_to_results,
        reached_results=bool(shown),
        shown=tuple(shown),
        found_nothing=any("ничего не нашлось" in reply.text for reply in sent),
        stub_replies=tuple(text for text in (reply.text for reply in sent) if _is_stub(text)),
        silent_steps=tuple(silent),
        passport_fields=_fields(_current(store)),
        replies=tuple(reply.text for reply in sent),
    )


async def run_all(scenarios: Sequence[Scenario] = SCENARIOS) -> list[Metrics]:
    return [await run_scenario(scenario) for scenario in scenarios]


async def _play(talker: Conversation, step: Step, sent: list[Reply]) -> None:
    async def send(reply: Reply) -> None:
        sent.append(reply)

    if isinstance(step, Says):
        await talker.on_text(CLIENT, step.text, send)
    elif isinstance(step, Taps):
        # Код берётся у последнего заданного вопроса: клиент нажимает ту
        # клавиатуру, которую видит. Вопроса не было — уедет пустой код, и бот
        # ответит тем, чем отвечает на нажатие в пустоту. Это тоже наблюдение.
        code = next(
            (reply.question.code for reply in reversed(sent) if reply.question is not None), ""
        )
        await talker.on_answer(CLIENT, code, step.value, send)
    else:
        await talker.on_feedback(CLIENT, step.feedback, send)


def _current(store: MemoryStore) -> Passport | None:
    current = next((row for row in reversed(store.rows) if row.is_current), None)
    return current.passport if current else None


def _cards(text: str, index: dict[str, Lot], passport: Passport | None) -> list[Shown]:
    """Карточки одного ответа, оценённые паспортом, по которому их искали."""
    cards: list[Shown] = []
    for url in _HREF_RE.findall(text):
        lot = index.get(url)
        if lot is None:  # pragma: no cover — ссылки в карточках только наши
            continue
        cards.append(
            Shown(
                external_id=lot.item.external_id,
                reason="" if passport is None else off_target(lot, passport),
                stale=assess(lot.item.posted_at).is_stale,
            )
        )
    return cards


def _questions_before_cards(sent: Sequence[Reply]) -> int:
    asked = 0
    for reply in sent:
        if _HREF_RE.search(reply.text):
            return asked
        if reply.question is not None:
            asked += 1
    return asked


def _is_stub(text: str) -> bool:
    """Ответ, который не помог: ни карточек, ни вопроса, ни честного отказа по городу."""
    return any(
        mark in text for mark in ("Сначала напишите", "Уточнить больше нечего", "Не смог доискать")
    )


def _fields(passport: Passport | None) -> dict[str, object]:
    """Плоский снимок паспорта путями вида `budget.max` — теми же, что в сценарии."""
    if passport is None:
        return {}
    values: dict[str, object] = {}
    for name in ("intent", "category", "city"):
        value = getattr(passport, name)
        if value:
            values[name] = str(value)
    for name in ("min", "max"):
        amount = getattr(passport.budget, name)
        if amount is not None:
            values[f"budget.{name}"] = float(amount)
    if passport.budget.currency is not None:
        values["budget.currency"] = str(passport.budget.currency)
    # Период цены несёт срок аренды: «посуточно» → day, «длительный срок» → month
    # (passport.md, «Прокат — аренда»). Всегда задан (по умолчанию month), поэтому
    # ассертить его стоит там, где он ОТЛИЧАЕТСЯ от умолчания.
    values["budget.period"] = str(passport.budget.period)
    for key, value in passport.attributes.items():
        values[f"attributes.{key}"] = value
    return values
