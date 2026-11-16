"""One bounded subscription job; never fall back while its outcome is uncertain."""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import uuid4

import httpx


class PrimaryStopped(RuntimeError):
    def __init__(self, reason: Literal["cap", "access", "uncertain"]) -> None:
        self.reason = reason
        super().__init__(f"subscription attempt stopped: {reason}")


def _guard(response: httpx.Response) -> None:
    if response.status_code >= 400 and "daily budget cap reached" in response.text:
        raise PrimaryStopped("cap")
    if response.status_code in {401, 403}:
        raise PrimaryStopped("access")


async def attempt(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    capability: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    cancel_timeout_s: float,
) -> tuple[dict[str, Any], int] | None:
    """None permits the old route; access/cap/ambiguous submit never do.

    max_tokens/temperature are deliberately absent: the subscription contract
    does not forward them. The original API payload retains both controls.
    """
    headers = {"X-Project-Key": key}
    job_id: int | None = None
    try:
        async with asyncio.timeout(timeout_s):
            response = await client.post(
                f"{base_url}/v1/jobs",
                params={"capability": capability},
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json=payload,
            )
            _guard(response)
            # These are explicit admission rejections, before a job was created.
            if response.status_code in {400, 404, 422, 429, 503}:
                return None
            if response.status_code != 202:
                raise PrimaryStopped("uncertain")
            value = response.json().get("job_id")
            if type(value) is not int or value <= 0:
                raise PrimaryStopped("uncertain")
            job_id = value
            while True:
                response = await client.get(f"{base_url}/v1/jobs/{job_id}", headers=headers)
                _guard(response)
                response.raise_for_status()
                body = response.json()
                if body.get("status") == "done":
                    return body, job_id
                if body.get("status") in {"error", "failed", "cancelled"}:
                    if "daily budget cap reached" in str(body.get("error", "")):
                        raise PrimaryStopped("cap")
                    return None
                await asyncio.sleep(min(2.0, max(0.1, float(body.get("poll_after_s", 2)))))
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
        if job_id is None:
            # The submit may have reached Broker. Never resubmit or start a paid
            # fallback without a job id with which to resolve that uncertainty.
            raise PrimaryStopped("uncertain") from None
    except asyncio.CancelledError as cancelled:
        if job_id is not None:
            try:
                await _cancel(client, base_url, headers, job_id, cancel_timeout_s)
            except PrimaryStopped as exc:
                # Preserve caller cancellation, with the unresolved job noted.
                raise cancelled from exc
        raise
    # A known job must become terminal before the old route may start. A mere
    # cancel_requested means the CLI can still be running and is insufficient.
    await _cancel(client, base_url, headers, job_id, cancel_timeout_s)
    return None


async def _cancel(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    job_id: int,
    timeout_s: float,
) -> None:
    try:
        async with asyncio.timeout(timeout_s):
            response = await client.post(f"{base_url}/v1/jobs/{job_id}/cancel", headers=headers)
            _guard(response)
            response.raise_for_status()
            while True:
                response = await client.get(f"{base_url}/v1/jobs/{job_id}", headers=headers)
                _guard(response)
                response.raise_for_status()
                body = response.json()
                if body.get("status") in {"error", "failed"} and (
                    "daily budget cap reached" in str(body.get("error", ""))
                ):
                    raise PrimaryStopped("cap")
                if body.get("status") in {"done", "error", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.1)
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise PrimaryStopped("uncertain") from None
