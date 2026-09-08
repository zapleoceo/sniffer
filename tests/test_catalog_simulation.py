"""The simulation exercises the real request-scoped catalogue boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sniffer.bot.store import Client
from sniffer.domain.passport import Passport
from sniffer.simulation.catalog_harness import (
    CATALOG_SCENARIOS,
    DeterministicCatalogSearch,
    catalog_faults,
    run_catalog_all,
    run_catalog_scenario,
)
from sniffer.simulation.stubs import MemoryStore


async def test_personas_use_owned_identity_and_never_over_question() -> None:
    runs = await run_catalog_all()
    assert {run.scenario.key for run in runs} == {
        "ru_buy",
        "en_buy",
        "vi_buy",
        "rent_room",
        "sell_bike",
        "rent_out",
        "refinement",
        "topic_switch",
        "one_question",
    }
    for run in runs:
        assert run.calls
        assert len(run.questions) <= 1
        assert all(call.user_id == 1 and call.allow_collection for call in run.calls)
        assert all(call.root > 0 and call.version > 0 for call in run.calls)
        assert run.transcript[0].actor == "client"
        assert any(turn.actor == "bot" for turn in run.transcript)
        assert not catalog_faults(run)

    calls = {run.scenario.key: run.calls[-1] for run in runs}
    assert calls["ru_buy"].scope.sources == ("chotot", "archive")
    assert calls["rent_room"].scope.sources == ("archive",)
    assert calls["sell_bike"].scope.sources == ("archive",)


@pytest.mark.parametrize(
    ("key", "deal_type", "external_id"),
    [
        ("ru_buy", "sell", "nt-lead"),
        ("en_buy", "sell", "dn-vision"),
        ("vi_buy", "sell", "nt-vision-budget"),
        ("rent_room", "rent_out", "dn-room"),
        ("sell_bike", "wanted", "nt-wanted-vision"),
        ("rent_out", "wanted", "nt-wanted-apartment"),
    ],
)
async def test_language_and_intent_personas_get_counterpart_catalogue(
    key: str, deal_type: str, external_id: str
) -> None:
    scenario = next(item for item in CATALOG_SCENARIOS if item.key == key)
    run = await run_catalog_scenario(scenario)
    call = run.calls[-1]
    assert call.scope.deal_type == deal_type
    assert external_id in call.external_ids
    assert any(external_id in turn.text for turn in run.transcript if turn.actor == "bot")


async def test_refinement_keeps_root_and_advances_exact_version() -> None:
    scenario = next(item for item in CATALOG_SCENARIOS if item.key == "refinement")
    run = await run_catalog_scenario(scenario)
    assert [(call.root, call.version) for call in run.calls] == [(1, 1), (1, 2)]
    assert run.calls[0].scope.criteria.budget_max == 1_000
    assert run.calls[1].scope.criteria.budget_max == 700
    assert run.calls[0].external_ids == ("dn-yamaha-budget", "dn-yamaha-pricey")
    assert run.calls[1].external_ids == ("dn-yamaha-budget",)


async def test_topic_switch_creates_independent_owned_roots() -> None:
    scenario = next(item for item in CATALOG_SCENARIOS if item.key == "topic_switch")
    run = await run_catalog_scenario(scenario)
    assert [(call.root, call.version) for call in run.calls] == [(1, 1), (2, 1)]
    assert [call.scope.category for call in run.calls] == ["motorbike", "room"]
    assert [call.scope.city for call in run.calls] == ["nha_trang", "da_nang"]


async def test_only_unknown_category_asks_one_question_then_uses_revision() -> None:
    scenario = next(item for item in CATALOG_SCENARIOS if item.key == "one_question")
    run = await run_catalog_scenario(scenario)
    assert run.questions == ("category",)
    assert [(call.root, call.version) for call in run.calls] == [(1, 2)]
    assert run.calls[0].scope.criteria.engine_cc == 125
    assert run.calls[0].scope.criteria.engine_cc_dir == "max"


async def test_catalog_fake_rejects_unowned_or_unknown_version() -> None:
    store = MemoryStore()
    search = DeterministicCatalogSearch(store)
    with pytest.raises(LookupError, match="catalog_identity_not_owned"):
        await search(1, 999, 1, allow_collection=True)
    dialogue = await store.load(Client(tg_user_id=888))
    await store.start(dialogue, Passport(raw_query="anything"))
    with pytest.raises(LookupError, match="catalog_identity_not_owned"):
        await search(2, 1, 1, allow_collection=True)
    with pytest.raises(LookupError, match="catalog_identity_not_owned"):
        await search(1, 1, 2, allow_collection=True)


async def test_catalog_verdict_rejects_a_missing_search() -> None:
    run = await run_catalog_scenario(CATALOG_SCENARIOS[0])

    assert "searches=0, expected=1" in catalog_faults(replace(run, calls=()))
