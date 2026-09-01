"""Учёт расходов брокера — без сети и без базы.

Проверяется главное свойство: расход привязывается к запросу клиента ключом
(`request_id` брокера + наш `client_requests.id`), а не сопоставлением времени.
При двух параллельных запросах время врёт, и именно это ловят тесты ниже.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

from sniffer.broker import usage
from sniffer.broker.client import BrokerClient, BrokerResult
from sniffer.broker.contracts import UsageSink
from sniffer.domain.records import BrokerCall

DONE = {
    "status": "done",
    "text": "ответ",
    "provider": "groq",
    "model": "llama-3.3-70b",
    "tokens_in": 120,
    "tokens_out": 45,
    "cost_usd": 0.000123,
    "latency_ms": 870,
    "request_id": 4242,
}


def transport(body: dict[str, Any]) -> httpx.MockTransport:
    """Брокер: submit отдаёт job_id, первый же поллинг — готовый ответ."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": 7})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


def client(body: dict[str, Any], sink: UsageSink) -> BrokerClient:
    return BrokerClient(
        httpx.AsyncClient(transport=transport(body), base_url="https://broker.test"),
        usage=sink,
    )


class Recorder:
    def __init__(self) -> None:
        self.calls: list[BrokerCall] = []

    async def __call__(self, capability: str, result: BrokerResult) -> None:
        self.calls.append(usage.to_broker_call(capability, result))


async def test_result_carries_the_accounting_fields() -> None:
    """Без request_id связать запрос и расход можно только по времени."""
    recorder = Recorder()

    result = await client(DONE, recorder).chat([{"role": "user", "content": "привет"}])

    assert result.request_id == 4242
    assert (result.tokens_in, result.tokens_out) == (120, 45)
    assert result.model == "llama-3.3-70b"
    assert result.latency_ms == 870


async def test_call_is_linked_to_the_open_client_request() -> None:
    recorder = Recorder()

    with usage.request_scope(11):
        await client(DONE, recorder).chat([{"role": "user", "content": "привет"}])

    call = recorder.calls[0]
    assert (call.request_id, call.broker_request_id) == (11, 4242)
    assert call.capability == "chat:fast"
    assert call.cost_usd == Decimal("0.000123")


async def test_parallel_requests_do_not_mix_up_costs() -> None:
    """Тот самый случай, из-за которого связь по времени неверна."""
    recorder = Recorder()

    async def ask(request_id: int, cost: float) -> None:
        with usage.request_scope(request_id):
            body = DONE | {"cost_usd": cost, "request_id": request_id * 100}
            await client(body, recorder).chat([{"role": "user", "content": "x"}])

    await asyncio.gather(ask(1, 0.001), ask(2, 0.002))

    by_request = {call.request_id: call.cost_usd for call in recorder.calls}
    assert by_request == {1: Decimal("0.001"), 2: Decimal("0.002")}


async def test_costs_use_decimal_not_float() -> None:
    """Суммы по сотне вызовов не должны расходиться с брокером в младших знаках."""
    call = usage.to_broker_call("structured", BrokerResult(text="", cost_usd=0.1))

    assert call.cost_usd == Decimal("0.1")


async def test_missing_broker_numbers_do_not_break_the_answer() -> None:
    """Провайдер без учётных полей — это ноль в учёте, а не падение вызова."""
    recorder = Recorder()
    body = {"status": "done", "text": "ответ", "tokens_in": None, "request_id": "не число"}

    result = await client(body, recorder).chat([{"role": "user", "content": "x"}])

    assert result.text == "ответ"
    assert recorder.calls[0].broker_request_id is None
    assert recorder.calls[0].tokens_in == 0


async def test_broken_accounting_does_not_lose_a_paid_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ответ уже оплачен: падать из-за недоступной базы значит его выбросить."""

    async def failing(capability: str, result: BrokerResult) -> None:
        raise RuntimeError("база недоступна")

    result = await client(DONE, failing).chat([{"role": "user", "content": "x"}])

    assert result.text == "ответ"


async def test_scope_is_reset_even_on_error() -> None:
    with pytest.raises(RuntimeError), usage.request_scope(5):
        raise RuntimeError("поиск упал")

    assert usage.current_request_id() is None
