from __future__ import annotations

import httpx

from sniffer.search.currency import clear_rate_cache, usd_vnd_rate


async def test_usd_vnd_rate_reads_documented_response() -> None:
    clear_rate_cache()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"rate": 26_316.0}))
    )
    try:
        assert await usd_vnd_rate(client) == 26_316.0
    finally:
        await client.aclose()


async def test_broken_rate_does_not_break_search() -> None:
    clear_rate_cache()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    try:
        assert await usd_vnd_rate(client) is None
    finally:
        await client.aclose()
