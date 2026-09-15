"""Turn a completed collection task into one durable answer per current requester."""

from __future__ import annotations

import structlog

from sniffer.agent_app.main import search_request
from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.collection_tasks import (
    CollectionLease,
    CollectionRecipient,
    CollectionTaskRepository,
)
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)


async def queue_answers(lease: CollectionLease) -> int:
    """Analyse fresh catalogue state, then enqueue a terminal client answer."""
    async with session_scope() as session:
        recipients = await CollectionTaskRepository(session).pending_recipients(
            lease.id, lease.token
        )

    if not recipients:
        return 0
    # One task is one canonical scope. Analysing it once is sufficient for all
    # subscribers and prevents shared demand from multiplying broker cost.
    payload = await _answer(recipients[0])
    queued = 0
    for recipient in recipients:
        async with session_scope() as session:
            added = await CollectionTaskRepository(session).queue_reply(
                lease.id, lease.token, recipient, payload
            )
            await session.commit()
        queued += int(added)
    return queued


async def _answer(recipient: CollectionRecipient) -> dict[str, object]:
    try:
        answer = await search_request(
            recipient.user_id,
            recipient.request_id,
            recipient.request_version,
            allow_collection=False,
        )
    except Exception as exc:
        log.exception(
            "collector.reply_analysis_failed",
            user_id=recipient.user_id,
            request_id=recipient.request_id,
            kind=type(exc).__name__,
        )
        return _payload(
            "Обновление каталога завершено, но проверить результаты не удалось. "
            "Повторите запрос через несколько минут.",
            [],
        )
    if not answer.items:
        if answer.status:
            return _payload(f"Обновление каталога завершено. {answer.status}", [])
        return _payload(
            "Обновление каталога завершено. Подходящих вариантов по вашему запросу "
            "пока нет. Можно изменить условия или включить мониторинг через /requests.",
            [],
        )
    count = min(len(answer.items), get_settings().max_cards)
    word = "вариант" if count == 1 else "варианта" if 2 <= count <= 4 else "вариантов"
    return _payload(
        f"Обновление каталога завершено. Нашёл {count} подходящих {word}:",
        answer.items[:count],
    )


def _payload(intro: str, items: list[RawItem]) -> dict[str, object]:
    return {
        "kind": "collection_result",
        "intro": intro,
        "items": [_item(item) for item in items],
    }


def _item(item: RawItem) -> dict[str, object]:
    return {
        "title": item.title,
        "summary": "",
        "url": item.url,
        "price_display": item.price_raw
        or (f"{item.price_vnd:,} ₫".replace(",", " ") if item.price_vnd else ""),
        "posted_at": item.posted_at.isoformat() if item.posted_at else "",
    }
