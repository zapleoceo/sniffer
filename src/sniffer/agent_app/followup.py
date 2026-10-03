"""Turn a completed collection task into one durable answer per current requester."""

from __future__ import annotations

import structlog

from sniffer.agent_app.followup_quota import Cut, FoundCards, cut_for
from sniffer.agent_app.main import search_request
from sniffer.bot import wording_plan
from sniffer.bot.quota import QuotaService
from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.collection_tasks import (
    CollectionLease,
    CollectionRecipient,
    CollectionTaskRepository,
)
from sniffer.domain.card_facts import facts_line
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)


async def queue_answers(lease: CollectionLease, *, quota: QuotaService | None = None) -> int:
    """Analyse fresh catalogue state, then enqueue a terminal client answer."""
    return await queue_answers_for_outcome(lease, collection_succeeded=True, quota=quota)


async def queue_failure_answers(
    lease: CollectionLease, *, quota: QuotaService | None = None
) -> int:
    """Answer even when collection failed, using the catalogue already available."""
    return await queue_answers_for_outcome(lease, collection_succeeded=False, quota=quota)


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


async def queue_answers_for_outcome(
    lease: CollectionLease, *, collection_succeeded: bool, quota: QuotaService | None = None
) -> int:
    async with session_scope() as session:
        recipients = await CollectionTaskRepository(session).pending_recipients(
            lease.id, lease.token
        )

    if not recipients:
        return 0
    # One task is one canonical scope. Analysing it once is sufficient for all
    # subscribers and prevents shared demand from multiplying broker cost.
    payload, found = await _answer(recipients[0], collection_succeeded=collection_succeeded)
    payload["collection_task_id"] = lease.id
    return await queue_payload_for_recipients(lease, recipients, payload, found=found, quota=quota)


async def queue_payload_for_recipients(
    lease: CollectionLease,
    recipients: list[CollectionRecipient],
    payload: dict[str, object],
    *,
    found: FoundCards | None = None,
    quota: QuotaService | None = None,
) -> int:
    """Durably enqueue one prepared payload for each still-current recipient.

    With a quota every recipient gets the payload cut to what the limit lets *them* see:
    the analysis is shared, the allowance is not. A reservation is confirmed only when
    the reply really entered the outbox and handed back otherwise.
    """
    queued = 0
    for recipient in recipients:
        async with session_scope() as session:
            mine, share = payload, None
            if quota is not None and found is not None:
                share = await cut_for(quota, session, recipient, found)
                if share is not None:
                    mine = {**payload, **_quota_view(found, share)}
            try:
                added = await CollectionTaskRepository(session).queue_reply(
                    lease.id,
                    lease.token,
                    recipient,
                    {
                        **mine,
                        "request_id": recipient.request_id,
                        "request_version": recipient.request_version,
                    },
                )
                await session.commit()
            except BaseException:
                # Not queued, so not shown: the slots this reservation took go back.
                if quota is not None and share is not None:
                    await quota.release(share.admission)
                raise
        if quota is not None and share is not None:
            await (quota.confirm(share.admission) if added else quota.release(share.admission))
        queued += int(added)
    return queued


def _quota_view(found: FoundCards, share: Cut) -> dict[str, object]:
    """Intro and cards after the quota: the same words the chat uses, minus the buttons."""
    admission, renews = share.admission, share.admission.period_end
    if not share.shown:
        if admission.limit != FREE_CARDS_PER_PERIOD:
            text = wording_plan.exhausted_cap(total=found.total, renews=renews)
        elif not get_settings().selling:
            text = wording_plan.exhausted_closed(total=found.total, renews=renews)
        elif share.offer:
            text = wording_plan.exhausted_offer(total=found.total, renews=renews)
        else:
            text = wording_plan.exhausted_short(total=found.total, renews=renews)
        return {"intro": f"{found.prefix}\n{text}", "items": []}
    lines = [found.prefix]
    balance = wording_plan.balance_line(admission.limit, admission.remaining, renews)
    if balance:
        lines.append(balance)
    count = len(share.shown)
    word = "вариант" if count == 1 else "варианта" if 2 <= count <= 4 else "вариантов"
    lines.append(f"Нашёл {count} подходящих {word}:")
    if admission.withheld:
        lines.append(
            wording_plan.more_line(
                found.total - count,
                limit=admission.limit,
                renews=renews,
                selling=get_settings().selling,
            )
        )
    return {"intro": "\n".join(lines), "items": [_item(item) for item in share.shown]}


async def _answer(
    recipient: CollectionRecipient, *, collection_succeeded: bool
) -> tuple[dict[str, object], FoundCards | None]:
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
        return (
            _payload(
                f"{prefix} Проверить результаты не удалось. "
                "Повторите запрос через несколько минут.",
                [],
            ),
            None,
        )
    if not answer.items:
        if answer.status:
            return _payload(f"{prefix} {answer.status}", []), None
        return (
            _payload(
                f"{prefix} Подходящих вариантов по вашему запросу пока нет. "
                "Можно изменить условия или включить мониторинг через /requests.",
                [],
            ),
            None,
        )
    count = min(len(answer.items), get_settings().max_cards)
    word = "вариант" if count == 1 else "варианта" if 2 <= count <= 4 else "вариантов"
    page = answer.items[:count]
    return (
        _payload(f"{prefix} Нашёл {count} подходящих {word}:", page),
        FoundCards(prefix=prefix, items=page, total=len(answer.items)),
    )


def _payload(intro: str, items: list[RawItem]) -> dict[str, object]:
    return {
        "kind": "collection_result",
        "intro": intro,
        "items": [_item(item) for item in items],
    }


def _item(item: RawItem) -> dict[str, object]:
    attributes = item.raw.get("attributes")
    return {
        "title": item.title,
        "facts": facts_line(attributes if isinstance(attributes, dict) else None, title=item.title),
        "summary": "",
        "url": item.url,
        "price_display": item.price_raw
        or (f"{item.price_vnd:,} ₫".replace(",", " ") if item.price_vnd else ""),
        "posted_at": item.posted_at.isoformat() if item.posted_at else "",
    }
