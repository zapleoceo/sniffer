"""Мелочи панели и тем: имя темы, подпись поля, предохранитель нотифаера «темы выключены»."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sniffer.bot import filter_card, tab_flow
from sniffer.bot.store import Client
from sniffer.bot.threads import title
from sniffer.config import Settings
from sniffer.domain.field_spec import FieldSpec, Kind, Monitor
from sniffer.domain.passport import Category, Passport
from sniffer.notifier import ports
from tests.test_topics import FakeBot, as_bot


def test_a_field_label_is_escaped_in_the_choice_prompt() -> None:
    spec = FieldSpec(
        key="x", path="attributes.x", label="A<b>&", kind=Kind.CHOICE, monitor=Monitor.NO
    )
    text = filter_card.field_text(spec)
    assert "A&lt;b&gt;&amp;" in text and "<b>A<b>" not in text


# ── переименование темы ─────────────────────────────────────────────────────


class Tabs:
    def __init__(self, shown: str | None) -> None:
        self.shown = shown
        self.titles: list[str] = []

    async def tab_of(self, _user: int, _root: int) -> tuple[int, str | None, str]:
        return 7, self.shown, "open"

    async def set_title(self, _user: int, _root: int, name: str) -> None:
        self.titles.append(name)


class Store:
    async def load(self, _client: Client) -> Any:
        stored = SimpleNamespace(
            passport=Passport(category=Category.MOTORBIKE, city="nha_trang"), root=5
        )
        return SimpleNamespace(user_id=1, passport=stored)


def _wire(monkeypatch: pytest.MonkeyPatch, tabs: Tabs) -> None:
    class Session:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(tab_flow, "session_scope", scope)
    monkeypatch.setattr(tab_flow, "TabRepository", lambda _session: tabs)


async def test_a_topic_is_renamed_only_when_the_title_really_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = Tabs("старое имя")
    _wire(monkeypatch, probe)
    bot = FakeBot()
    client = Client(tg_user_id=9, thread_id=7)

    await tab_flow.sync_title(as_bot(bot), client, cast(Any, Store()))
    assert len(bot.renamed) == 1
    same = title(Passport(category=Category.MOTORBIKE, city="nha_trang"), limit=64)
    probe.shown = same

    await tab_flow.sync_title(as_bot(bot), client, cast(Any, Store()))

    assert len(bot.renamed) == 1, "имя совпало: Telegram не дёргаем"
    assert probe.titles == [same]


# ── нотифаер: темы выключены флагом ─────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_notifier_reads_thread_links_only_when_topics_are_enabled(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    @asynccontextmanager
    async def scope() -> AsyncIterator[Any]:
        yield SimpleNamespace(commit=None)

    monkeypatch.setattr(ports, "session_scope", scope)
    monkeypatch.setattr(ports, "get_settings", lambda: Settings(topics_enabled=enabled))
    monkeypatch.setattr(ports, "DeliveryRepository", lambda _s: object())
    monkeypatch.setattr(ports, "UserRepository", lambda _s: object())
    monkeypatch.setattr(ports, "TabRepository", lambda _s: "tabs")

    async with ports.work_scope() as work:
        assert (cast(Any, work.tabs) == "tabs") is enabled
