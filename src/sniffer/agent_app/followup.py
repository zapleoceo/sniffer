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
    return await queue_answers_for_outcome(lease, collection_succeeded=True)


async def queue_failure_answers(lease: CollectionLease) -> int:
    """Answer even when collection failed, using the catalogue already available."""
    return await queue_answers_for_outcome(lease, collection_succeeded=False)


async def queue_cap_answers(lease: CollectionLease) -> int:
    """Queue a deterministic answer without calling the capped broker."""
    async with session_scope() as session:
        recipients = await CollectionTaskRepository(session).pending_recipients(
            lease.id, lease.token
        )
    if not recipients:
        return 0
    payload = _payload(
        "Сейчас не удалось проверить обновлённый каталог. "
        "Повторите запрос позже — сохранённые условия останутся доступны через /requests.",
        [],
    )
    payload["collection_task_id"] = lease.id
    return await queue_payload_for_recipients(lease, recipients, payload)


async def queue_answers_for_outcome(lease: CollectionLease, *, collection_succeeded: bool) -> int:
    async with session_scope() as session:
        recipients = await CollectionTaskRepository(session).pending_recipients(
            lease.id, lease.token
        )

    if not recipients:
        return 0
    # One task is one canonical scope. Analysing it once is sufficient for all
    # subscribers and prevents shared demand from multiplying broker cost.
    payload = await _answer(recipients[0], collection_succeeded=collection_succeeded)
    payload["collection_task_id"] = lease.id
    return await queue_payload_for_recipients(lease, recipients, payload)


async def queue_payload_for_recipients(
    lease: CollectionLease,
    recipients: list[CollectionRecipient],
    payload: dict[str, object],
) -> int:
    """Durably enqueue one prepared payload for each still-current recipient."""
    queued = 0
    for recipient in recipients:
        async with session_scope() as session:
            added = await CollectionTaskRepository(session).queue_reply(
                lease.id,
                lease.token,
                recipient,
                {
                    **payload,
                    "request_id": recipient.request_id,
                    "request_version": recipient.request_version,
                },
            )
            await session.commit()
        queued += int(added)
    return queued


async def _answer(
    recipient: CollectionRecipient, *, collection_succeeded: bool
) -> dict[str, object]:
    prefix = (
        "Обновление каталога завершено."
        if collection_succeeded
        else "Полностью обновить каталог не удалось. Проверил уже собранные данные."
    )
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
            f"{prefix} Проверить результаты не удалось. Повторите запрос через несколько минут.",
            [],
        )
    if not answer.items:
        if answer.status:
            return _payload(f"{prefix} {answer.status}", [])
        return _payload(
            f"{prefix} Подходящих вариантов по вашему запросу пока нет. "
            "Можно изменить условия или включить мониторинг через /requests.",
            [],
        )
    count = min(len(answer.items), get_settings().max_cards)
    word = "вариант" if count == 1 else "варианта" if 2 <= count <= 4 else "вариантов"
    return _payload(
        f"{prefix} Нашёл {count} подходящих {word}:",
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
