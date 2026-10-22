"""Отклонённые строки `raw_messages` пишутся с минимальным `gate_signals`.

Без живой базы: репозитории подменены, проверяется ровно то, что воронка
передаёт в `set_stage`. Полные сигналы extracted/duplicate закрыты тестами
`test_db_repositories.py` (боевой Postgres в CI).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from sniffer.domain.records import RawMessage
from sniffer.worker import archive as module


class _Recorder:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, session: object) -> None:
        pass

    async def set_stage(self, ids: list[int], stage: str, **kwargs: Any) -> None:
        type(self).calls.append({"ids": ids, "stage": stage, **kwargs})


class _NoChat:
    def __init__(self, session: object) -> None:
        pass

    async def get_by_tg_id(self, tg_id: int) -> None:
        return None


def _raw(text: str) -> RawMessage:
    return RawMessage(
        id=5,
        chat_tg_id=-100123,
        msg_id=1,
        text=text,
        text_hash="h",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[_Recorder]:
    _Recorder.calls = []
    monkeypatch.setattr(module, "RawMessageRepository", _Recorder)
    monkeypatch.setattr(module, "ChatRepository", _NoChat)
    return _Recorder


async def test_gate_rejection_stores_only_the_reason(recorder: type[_Recorder]) -> None:
    await module.ArchivePipeline()._one(_raw("Ищу байк в аренду на месяц, бюджет 100$"), None)  # type: ignore[arg-type]

    assert recorder.calls == [
        {"ids": [5], "stage": "rejected", "gate_signals": {"reason": "demand_not_offer"}}
    ]


async def test_unknown_chat_rejection_stores_only_the_reason(recorder: type[_Recorder]) -> None:
    await module.ArchivePipeline()._one(_raw("Продам Honda Vision 2021, цена 15.000.000 VND"), None)  # type: ignore[arg-type]

    assert recorder.calls == [
        {"ids": [5], "stage": "rejected", "gate_signals": {"reason": "unknown_chat"}}
    ]


async def test_rejection_touches_only_stage_and_signals(recorder: type[_Recorder]) -> None:
    """Текст и отпечаток не трогаются: уходят лишь стадия и `reason`."""
    await module.ArchivePipeline()._one(_raw("Ищу байк в аренду на месяц, бюджет 100$"), None)  # type: ignore[arg-type]

    assert set(recorder.calls[0]) == {"ids", "stage", "gate_signals"}
