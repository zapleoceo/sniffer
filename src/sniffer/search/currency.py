"""Курс валюты для бюджета, без которого долларовый запрос нельзя честно сузить."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import structlog

log = structlog.get_logger(__name__)

RATE_URL = "https://api.frankfurter.dev/v2/rate/USD/VND"
RATE_TTL = timedelta(hours=6)
RATE_TIMEOUT_S = 3.0

_cached_rate: float | None = None
_cached_at: datetime | None = None


async def usd_vnd_rate(client: httpx.AsyncClient | None = None) -> float | None:
    """Вернуть свежий VND за USD или ``None`` без исключения наружу."""
    global _cached_rate, _cached_at
    now = datetime.now(UTC)
    if _cached_rate is not None and _cached_at is not None and now - _cached_at < RATE_TTL:
        return _cached_rate

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=RATE_TIMEOUT_S)
    try:
        response = await http.get(RATE_URL)
        response.raise_for_status()
        payload = response.json()
        rate = payload.get("rate") if isinstance(payload, dict) else None
        if isinstance(rate, bool) or not isinstance(rate, int | float) or rate <= 0:
            raise ValueError("rate is absent or invalid")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("currency.usd_vnd_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        if owns_client:
            await http.aclose()

    _cached_rate = float(rate)
    _cached_at = now
    return _cached_rate


def clear_rate_cache() -> None:
    """Сброс для тестов."""
    global _cached_rate, _cached_at
    _cached_rate = None
    _cached_at = None
