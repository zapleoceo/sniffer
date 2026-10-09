"""Шаг «снять превью кандидатов очереди» - отдельный от вступления и выключенный по умолчанию.

Ни к Telegram, ни к лимитам вступлений он не прикасается: читает публичную страницу
и пишет класс в `chat_candidates`. Ограничения частоты два и оба в конфиге:
не чаще одного запроса в `join_preview_interval_s` секунд и не больше
`join_preview_per_pass` запросов за проход. Ошибка сети - `unknown`, без повтора.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog

from sniffer.config import Settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories import CandidateRepository
from sniffer.domain.chat_preview import PreviewSnapshot
from sniffer.search.chat_preview import classify_preview
from sniffer.sources.chat_preview_fetch import fetch_preview

log = structlog.get_logger(__name__)

Fetcher = Callable[[str], Awaitable[PreviewSnapshot]]
Sleeper = Callable[[float], Awaitable[None]]


async def scan_previews(
    settings: Settings,
    *,
    fetch: Fetcher | None = None,
    sleep: Sleeper = asyncio.sleep,
) -> int:
    """Снять превью у не проверенных кандидатов; вернуть, сколько снято."""
    if not settings.join_preview_scan_enabled:
        return 0
    async with session_scope() as session:
        todo = await CandidateRepository(session).unchecked(limit=settings.join_preview_per_pass)
    if not todo:
        return 0
    async with httpx.AsyncClient() as client:

        async def default_fetch(username: str) -> PreviewSnapshot:
            return await fetch_preview(client, username)

        getter = fetch or default_fetch
        done = 0
        for index, (key, username) in enumerate(todo):
            if index:
                await sleep(settings.join_preview_interval_s)
            snapshot = await getter(username)
            verdict = classify_preview(snapshot)
            async with session_scope() as session:
                await CandidateRepository(session).save_preview(
                    key, cls=verdict.cls, evidence=verdict.evidence, snapshot=snapshot.to_json()
                )
                await session.commit()
            done += 1
    log.info("collector.previews_scanned", count=done)
    return done
