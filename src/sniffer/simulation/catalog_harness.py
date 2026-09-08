"""Deterministic personas through ``Conversation -> CatalogFinder``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sniffer.agent_app.contracts import CollectionScope
from sniffer.agent_app.main_gateway import collection_scope
from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.store import Client
from sniffer.config import Settings
from sniffer.domain.dialogue import Feedback
from sniffer.domain.passport import Passport
from sniffer.search.intake_rules import parse_query
from sniffer.search.relevance import rank_items
from sniffer.simulation.catalog_market import CATALOG_LOTS, CatalogLot
from sniffer.simulation.script import Reacts, Says, Step, Taps
from sniffer.simulation.stubs import MemoryStore, SilentJournal

CLIENT = Client(tg_user_id=7307, username="catalog-persona")
USD_VND = 25_000.0


class _RulesIntake:
    async def parse(self, text: str) -> Passport:
        return parse_query(text, default_city="nha_trang")


@dataclass(frozen=True, slots=True)
class CatalogScenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    expected_searches: int = 1
    expected_questions: int = 0


@dataclass(frozen=True, slots=True)
class SearchCall:
    user_id: int
    root: int
    version: int
    allow_collection: bool
    scope: CollectionScope
    external_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    actor: str
    text: str


@dataclass(frozen=True, slots=True)
class CatalogRun:
    scenario: CatalogScenario
    transcript: tuple[TranscriptTurn, ...]
    calls: tuple[SearchCall, ...]
    roots: tuple[int, ...]
    versions: tuple[int, ...]
    questions: tuple[str, ...]


class DeterministicCatalogSearch:
    """Catalogue protocol fake whose authority is the server-owned store row."""

    def __init__(self, store: MemoryStore, lots: tuple[CatalogLot, ...] = CATALOG_LOTS) -> None:
        self.store, self.lots = store, lots
        self.calls: list[SearchCall] = []

    async def __call__(
        self, user_id: int, request_id: int, version: int, *, allow_collection: bool
    ) -> Found:
        matches = [
            row
            for row in self.store.rows
            if row.user_id == user_id and row.root == request_id and row.version == version
        ]
        if len(matches) != 1:
            raise LookupError("catalog_identity_not_owned")
        passport = matches[0].passport
        scope = collection_scope(passport)
        candidates = [
            lot.item
            for lot in self.lots
            if (lot.city, lot.category, lot.deal_type)
            == (scope.city, scope.category, scope.deal_type)
        ]
        items = rank_items(passport, candidates, usd_vnd=USD_VND)
        self.calls.append(
            SearchCall(
                user_id,
                request_id,
                version,
                allow_collection,
                scope,
                tuple(item.external_id for item in items),
            )
        )
        return Found(items, sources=("verified_catalog",), status="Проверенный каталог.")


async def _legacy_must_not_run(_passport: Passport) -> Found:
    raise AssertionError("catalog simulation reached legacy finder")


CATALOG_SCENARIOS: tuple[CatalogScenario, ...] = (
    CatalogScenario(
        "ru_buy",
        "RU: Honda Lead, автомат, бюджет",
        (Says("Honda Lead автомат в Нячанге до 500 USD"),),
    ),
    CatalogScenario(
        "en_buy",
        "EN: scooter in Da Nang",
        (Says("I need an automatic scooter in Da Nang under $500"),),
    ),
    CatalogScenario(
        "vi_buy",
        "VI: Honda dưới 10 triệu",
        (Says("Cần mua xe Honda ở Nha Trang dưới 10 triệu"),),
    ),
    CatalogScenario("rent_room", "аренда комнаты", (Says("сниму комнату в Дананге до 500 USD"),)),
    CatalogScenario("sell_bike", "продажа байка", (Says("продаю Honda Vision в Нячанге"),)),
    CatalogScenario("rent_out", "сдача квартиры", (Says("сдам меблированную квартиру в Нячанге"),)),
    CatalogScenario(
        "refinement",
        "сужение цены после выдачи",
        (Says("Yamaha motorbike manual в Дананге до 1000 USD"), Reacts(Feedback.PRICEY)),
        expected_searches=2,
    ),
    CatalogScenario(
        "topic_switch",
        "байк, затем новый запрос жилья",
        (Says("скутер в Нячанге до 500 USD"), Says("сниму комнату в Дананге до 500 USD")),
        expected_searches=2,
    ),
    CatalogScenario(
        "one_question",
        "неясна только категория",
        (Says("нужно до 125 кубов"), Taps("motorbike")),
        expected_questions=1,
    ),
)


async def run_catalog_scenario(scenario: CatalogScenario) -> CatalogRun:
    store = MemoryStore()
    search = DeterministicCatalogSearch(store)
    finder = CatalogFinder(
        legacy=_legacy_must_not_run,
        catalog=search,
        settings=lambda: Settings(catalog_mode="catalog"),
    )
    talker = Conversation(
        store,
        intake=_RulesIntake,
        finder=_legacy_must_not_run,
        scoped_finder=finder,
        recorder=SilentJournal(),
    )
    transcript: list[TranscriptTurn] = []
    questions: list[str] = []
    last_question_code: str | None = None
    for step in scenario.steps:
        transcript.append(TranscriptTurn("client", _step_text(step)))

        async def send(reply: Reply) -> None:
            nonlocal last_question_code
            transcript.append(TranscriptTurn("bot", reply.text))
            if reply.question is not None:
                questions.append(reply.question.field)
                last_question_code = reply.question.code

        if isinstance(step, Says):
            await talker.on_text(CLIENT, step.text, send)
        elif isinstance(step, Reacts):
            await talker.on_feedback(CLIENT, step.feedback, send)
        else:
            await talker.on_answer(CLIENT, last_question_code or "", step.value, send)
    return CatalogRun(
        scenario,
        tuple(transcript),
        tuple(search.calls),
        tuple(row.root for row in store.rows),
        tuple(row.version for row in store.rows),
        tuple(questions),
    )


async def run_catalog_all(
    scenarios: Sequence[CatalogScenario] = CATALOG_SCENARIOS,
) -> list[CatalogRun]:
    return [await run_catalog_scenario(scenario) for scenario in scenarios]


def render_catalog_run(run: CatalogRun) -> str:
    lines = [f"── {run.scenario.key}: {run.scenario.title}"]
    lines.extend(f"  {turn.actor}: {turn.text}" for turn in run.transcript)
    return "\n".join(lines)


def render_catalog_report(runs: Sequence[CatalogRun], *, replies: bool = False) -> str:
    broken = [run for run in runs if catalog_faults(run)]
    lines = [
        f"catalog-path scenarios: {len(runs)}",
        f"searches: {sum(len(run.calls) for run in runs)} · "
        f"questions: {sum(len(run.questions) for run in runs)} · faults: {len(broken)}",
    ]
    for run in runs:
        identities = ", ".join(f"root={call.root}/v{call.version}" for call in run.calls)
        faults = "; ".join(catalog_faults(run))
        lines.append(f"  {run.scenario.key}: {identities or 'no search'} · {faults or 'ok'}")
    if replies:
        lines.append("")
        lines.append("\n\n".join(render_catalog_run(run) for run in runs))
    return "\n".join(lines)


def catalog_faults(run: CatalogRun) -> tuple[str, ...]:
    """Observable failures which make the catalog CLI exit non-zero."""
    faults: list[str] = []
    if len(run.calls) != run.scenario.expected_searches:
        faults.append(f"searches={len(run.calls)}, expected={run.scenario.expected_searches}")
    if len(run.questions) != run.scenario.expected_questions:
        faults.append(f"questions={len(run.questions)}, expected={run.scenario.expected_questions}")
    if len(set(run.questions)) != len(run.questions):
        faults.append("repeated question")
    if any(not call.allow_collection for call in run.calls):
        faults.append("collection disabled")
    bot_texts = [turn.text for turn in run.transcript if turn.actor == "bot"]
    if any("Не смог доискать" in text for text in bot_texts):
        faults.append("search failed")
    if not any("href=" in text for text in bot_texts):
        faults.append("no cards")
    return tuple(faults)


def _step_text(step: Step) -> str:
    if isinstance(step, Says):
        return step.text
    if isinstance(step, Reacts):
        return f"[{step.feedback.value}]"
    return f"[{step.value}]"
