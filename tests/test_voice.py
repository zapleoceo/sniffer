"""Голосовой запрос: что бот делает с записью и чего не делает никогда."""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.bot.voice import MAX_VOICE_SECONDS, transcribe
from sniffer.broker.client import BrokerError


class FakeBroker:
    def __init__(self, text: str = "", boom: Exception | None = None) -> None:
        self.text = text
        self.boom = boom
        self.calls = 0

    async def transcribe(self, audio: bytes, **_: Any) -> str:
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        return self.text

    async def aclose(self) -> None:
        return None


async def audio() -> bytes | None:
    return b"ogg-opus-bytes"


async def test_a_short_voice_becomes_a_query() -> None:
    broker = FakeBroker("ищу скутер автомат до четырёхсот долларов")

    heard = await transcribe(duration=5, download=audio, broker=broker)  # type: ignore[arg-type]

    assert heard == "ищу скутер автомат до четырёхсот долларов"


async def test_a_long_recording_is_refused_before_downloading() -> None:
    """Файл на десять минут незачем даже тянуть — ни трафика, ни денег."""
    calls = 0

    async def counted() -> bytes | None:
        nonlocal calls
        calls += 1
        return b"x"

    broker = FakeBroker("что-то")

    assert await transcribe(duration=MAX_VOICE_SECONDS + 1, download=counted, broker=broker) is None  # type: ignore[arg-type]
    assert (calls, broker.calls) == (0, 0)


@pytest.mark.parametrize(
    "boom",
    [
        BrokerError("project lacks scope: llm:audio"),
        TimeoutError(),
        OSError("сеть"),
    ],
)
async def test_a_broken_transcription_is_not_a_broken_bot(boom: Exception) -> None:
    """Клиент слышит «напишите текстом», а не молчание и не трейсбек.

    Самая частая причина здесь — не поломка, а отсутствие права `llm:audio` у
    ключа проекта, и вести себя она обязана так же тихо.
    """
    assert await transcribe(duration=5, download=audio, broker=FakeBroker(boom=boom)) is None  # type: ignore[arg-type]


async def test_an_undownloadable_voice_does_not_reach_the_model() -> None:
    """Файл старше суток Telegram не отдаёт — платить за это незачем."""

    async def gone() -> bytes | None:
        raise RuntimeError("file is too old")

    broker = FakeBroker("что-то")

    assert await transcribe(duration=5, download=gone, broker=broker) is None  # type: ignore[arg-type]
    assert broker.calls == 0


async def test_an_oversized_file_is_dropped_before_the_model() -> None:
    async def huge() -> bytes | None:
        return b"x" * (3 * 1024 * 1024)

    broker = FakeBroker("что-то")

    assert await transcribe(duration=5, download=huge, broker=broker) is None  # type: ignore[arg-type]
    assert broker.calls == 0


async def test_silence_is_not_a_query() -> None:
    """Пустой транскрипт — не повод запускать поиск по пустоте."""
    assert await transcribe(duration=5, download=audio, broker=FakeBroker("   ")) is None  # type: ignore[arg-type]
