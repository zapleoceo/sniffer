"""Граница между строкой таблицы и доменом.

Всё, что знает одновременно про колонки и про доменные типы, живёт здесь и
больше нигде. Функции чистые — их проверяют обычные тесты, без базы.
"""

from __future__ import annotations

from typing import Any

from sniffer.db import models
from sniffer.domain.passport import Budget, Category, Intent, Passport, PassportStatus
from sniffer.domain.records import (
    BrokerCall,
    Chat,
    ClientRequest,
    DialogMessage,
    Job,
    Listing,
    PassportEvent,
    RawMessage,
    SessionState,
    StoredPassport,
    User,
)


def to_chat(row: models.Chat) -> Chat:
    return Chat(
        id=row.id,
        tg_id=row.tg_id,
        title=row.title,
        city=row.city,
        username=row.username,
        categories=list(row.categories),
        is_active=row.is_active,
        search_rank=row.search_rank,
        msg_count_24h=row.msg_count_24h,
        last_msg_id=row.last_msg_id,
        last_synced_at=row.last_synced_at,
        added_at=row.added_at,
    )


def to_raw_message(row: models.RawMessage) -> RawMessage:
    return RawMessage(
        id=row.id,
        chat_tg_id=row.chat_tg_id,
        msg_id=row.msg_id,
        text=row.text,
        text_hash=row.text_hash,
        posted_at=row.posted_at,
        seller_id=row.seller_id,
        has_media=row.has_media,
        stage=row.stage,
        gate_signals=dict(row.gate_signals),
        ingested_at=row.ingested_at,
    )


def raw_message_values(message: RawMessage) -> dict[str, Any]:
    """Колонки под вставку. `id` не подставляем — его выдаёт BIGSERIAL."""
    return {
        "chat_tg_id": message.chat_tg_id,
        "msg_id": message.msg_id,
        "seller_id": message.seller_id,
        "text": message.text,
        "text_hash": message.text_hash,
        "has_media": message.has_media,
        "posted_at": message.posted_at,
        "stage": message.stage,
        "gate_signals": message.gate_signals,
    }


def to_listing(row: models.Listing) -> Listing:
    return Listing(
        id=row.id,
        raw_message_id=row.raw_message_id,
        deal_type=row.deal_type,
        category=row.category,
        city=row.city,
        title=row.title,
        summary=row.summary,
        tg_link=row.tg_link,
        posted_at=row.posted_at,
        seller_id=row.seller_id,
        district=row.district,
        price_amount=row.price_amount,
        price_currency=row.price_currency,
        price_period=row.price_period,
        price_usd_month=row.price_usd_month,
        attributes=dict(row.attributes),
        lang=row.lang,
        confidence=row.confidence,
        extracted_at=row.extracted_at,
        is_active=row.is_active,
    )


def to_user(row: models.User) -> User:
    return User(
        id=row.id,
        tg_user_id=row.tg_user_id,
        username=row.username,
        lang=row.lang,
        is_blocked=row.is_blocked,
        created_at=row.created_at,
    )


def to_job(row: models.Job) -> Job:
    return Job(
        id=row.id,
        kind=row.kind,
        payload=dict(row.payload),
        attempts=row.attempts,
        run_after=row.run_after,
    )


def to_stored_passport(row: models.Passport) -> StoredPassport:
    return StoredPassport(
        id=row.id,
        user_id=row.user_id,
        version=row.version,
        root_id=row.root_id,
        is_current=row.is_current,
        created_at=row.created_at,
        passport=Passport(
            # В колонках лежат значения перечислений (`rent`, `apartment`).
            # Разворачиваем их явно: незнакомое значение обязано упасть здесь,
            # а не приехать в матчинг строкой, которую SQL молча не найдёт.
            intent=Intent(row.intent) if row.intent else None,
            category=Category(row.category) if row.category else None,
            city=row.city,
            districts=list(row.districts),
            budget=Budget.model_validate(row.budget or {}),
            attributes=dict(row.attributes),
            must_have=list(row.must_have),
            deal_breakers=list(row.deal_breakers),
            timeframe_from=row.timeframe_from,
            timeframe_to=row.timeframe_to,
            raw_query=row.raw_query,
            confidence=row.confidence,
            missing_fields=list(row.missing_fields),
            status=PassportStatus(row.status),
        ),
    )


def to_passport_event(row: models.PassportEvent) -> PassportEvent:
    return PassportEvent(
        id=row.id,
        passport_id=row.passport_id,
        kind=row.kind,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def to_client_request(row: models.ClientRequest) -> ClientRequest:
    return ClientRequest(
        id=row.id,
        user_id=row.user_id,
        raw_query=row.raw_query,
        status=row.status,
        passport_id=row.passport_id,
        # Значения этапов — миллисекунды. В JSONB могло приехать что угодно,
        # поэтому приводим здесь: дашборд обязан считать сумму, а не спорить о
        # типе на строке из базы.
        stages={str(key): int(value) for key, value in dict(row.stages).items()},
        plan_fallback=row.plan_fallback,
        sources=list(row.sources),
        result_count=row.result_count,
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
    )


def to_dialog_message(row: models.DialogMessage) -> DialogMessage:
    return DialogMessage(
        id=row.id,
        user_id=row.user_id,
        direction=row.direction,
        text=row.text,
        request_id=row.request_id,
        created_at=row.created_at,
    )


def to_broker_call(row: models.BrokerCall) -> BrokerCall:
    return BrokerCall(
        id=row.id,
        request_id=row.request_id,
        broker_request_id=row.broker_request_id,
        capability=row.capability,
        provider=row.provider,
        model=row.model,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        cost_usd=row.cost_usd,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
    )


def broker_call_values(call: BrokerCall) -> dict[str, Any]:
    """Колонки под вставку расхода. `id` выдаёт BIGSERIAL."""
    return {
        "request_id": call.request_id,
        "broker_request_id": call.broker_request_id,
        "capability": call.capability,
        "provider": call.provider,
        "model": call.model,
        "tokens_in": call.tokens_in,
        "tokens_out": call.tokens_out,
        "cost_usd": call.cost_usd,
        "latency_ms": call.latency_ms,
    }


def to_session_state(row: models.TelegramSession) -> SessionState:
    """Состояние сессии БЕЗ шифртекста.

    `session_enc` сюда не попадает намеренно: доменная запись уезжает в
    дашборд, а строка сессии нужна только коллектору и читается отдельным
    методом репозитория.
    """
    return SessionState(
        id=row.id,
        phone=row.phone,
        is_active=row.is_active,
        last_ok_at=row.last_ok_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def passport_values(passport: Passport) -> dict[str, Any]:
    """Паспорт → колонки `passports`.

    Перечисления кладём их значениями, а не именами: в базе лежит `rent`, и
    сравнение в SQL идёт с ним же. `mode="json"` у бюджета — потому что в
    JSONB не положить `Currency.USD`.
    """
    return {
        "status": passport.status.value,
        "intent": passport.intent.value if passport.intent else None,
        "category": passport.category.value if passport.category else None,
        "city": passport.city,
        "districts": list(passport.districts),
        "budget": passport.budget.model_dump(mode="json"),
        "attributes": dict(passport.attributes),
        "must_have": list(passport.must_have),
        "deal_breakers": list(passport.deal_breakers),
        "timeframe_from": passport.timeframe_from,
        "timeframe_to": passport.timeframe_to,
        "raw_query": passport.raw_query,
        "confidence": passport.confidence,
        "missing_fields": list(passport.missing_fields),
    }
