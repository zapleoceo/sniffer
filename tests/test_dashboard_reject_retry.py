"""Повтор отказа на странице «База»: кнопка, CSRF, доступ, статусы ответов.

База подменена на границе `dashboard/data.py` (как в `test_dashboard_pages.py`): здесь
проверяется веб-слой. Транзакции, замок и лимиты проверяет `test_reject_retry_db.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sniffer.dashboard import app as dashboard_app
from sniffer.dashboard import auth, data
from sniffer.domain import reject_retry
from sniffer.domain.records import RejectedCandidate
from sniffer.domain.reject_retry import (
    RetryCode,
    RetryDecision,
    RetryOffer,
    RetryRecord,
    RetryResult,
    RetryStatus,
)
from tests.conftest import OWNER, DashboardEnv

# Время страницы — «сейчас» самой страницы, не часов базы.
WHEN = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NASTY = '<script>alert("x")</script>'
TEMP = "too_many_attempts"

OK = RetryDecision(RetryCode.OK, "можно повторить")
COOLDOWN = RetryDecision(
    RetryCode.COOLDOWN, "между попытками должно пройти 24 ч", available_at=WHEN + timedelta(hours=9)
)


class Boom(Exception):
    """Исключение чужого типа: обработчик не должен знать его класс, чтобы ответить."""


def reject(key: str, reason: str = TEMP) -> RejectedCandidate:
    return RejectedCandidate(key=key, reason=reason, rejected_at=WHEN)


def inventory(**changes: Any) -> data.Inventory:
    base: dict[str, Any] = {
        "stats": {"chats": 1, "chats_active": 1, "raw_messages": 1},
        "rejects": [reject("@t_open"), reject("@t_wait"), reject("@perm", "user")],
        "reject_counts": {TEMP: 2, "user": 1},
        "temporary_rejects": [reject("@t_open"), reject("@t_wait"), reject("+Inv_ite-1")],
        "retry_offers": {
            "@t_open": RetryOffer(OK, attempts_used=1),
            "@t_wait": RetryOffer(COOLDOWN, attempts_used=1),
            "+Inv_ite-1": RetryOffer(OK),
        },
        "tracked_chats": 12,
        "chat_cap": 200,
    }
    return data.Inventory(**{**base, **changes})


@pytest.fixture(autouse=True)
def env(dashboard_env: DashboardEnv) -> DashboardEnv:
    return dashboard_env


class Calls:
    """Что дашборд спросил у базы: порядок и аргументы."""

    def __init__(self) -> None:
        self.retries: list[dict[str, Any]] = []
        self.view = inventory()
        self.result = RetryResult(RetryStatus.CREATED, OK)
        self.raises: Exception | None = None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    state = Calls()

    async def fake_inventory(**_: Any) -> data.Inventory:
        return state.view

    async def fake_retry(key: str, **kwargs: Any) -> RetryResult:
        state.retries.append({"key": key, **kwargs})
        if state.raises is not None:
            raise state.raises
        return state.result

    monkeypatch.setattr(data, "inventory", fake_inventory)
    monkeypatch.setattr(data, "request_retry", fake_retry)
    return state


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(dashboard_app.create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def owner(client: TestClient) -> TestClient:
    token, _ = auth.issue_session()
    client.cookies.set(auth.COOKIE_NAME, token)
    return client


def post(client: TestClient, key: str = "%40t_open", **form: str) -> Any:
    fields = {"csrf": auth.issue_csrf(), "request_id": "form-token-0001", **form}
    return client.post(f"/rejects/{key}/retry", data=fields, follow_redirects=False)


# ── что рисует страница ─────────────────────────────────────────────────────


def test_a_button_exists_only_for_a_temporary_reject_that_may_be_retried(
    owner: TestClient, calls: Calls
) -> None:
    body = owner.get("/database").text

    forms = re.findall(r"<form method='post' action='([^']+)'>", body)
    assert forms == ["/rejects/%40t_open/retry", "/rejects/%2BInv_ite-1/retry"]
    assert body.count("<button type='submit'>") == 2
    assert "между попытками должно пройти 24 ч" in body
    assert "доступно с 2026-10-10 21:00:00 UTC" in body
    assert body.count("<button disabled>") == 1


def test_every_form_carries_a_signed_csrf_token_and_a_fresh_form_token(
    owner: TestClient, calls: Calls
) -> None:
    body = owner.get("/database").text

    csrfs = re.findall(r"name='csrf' value='([^']+)'", body)
    forms = re.findall(r"name='request_id' value='([^']+)'", body)
    assert len(csrfs) == len(forms) == 2
    assert all(auth.is_valid_csrf(token) for token in csrfs)
    assert len(set(forms)) == 2, "у каждой формы свой токен идемпотентности"


def test_non_temporary_rejects_get_a_hint_and_no_controls(owner: TestClient, calls: Calls) -> None:
    calls.view = inventory(
        rejects=[
            reject("@a", "user"),
            reject("@b", "already_member"),
            reject("@c", "join_request_sent"),
            reject("@d", "unresolved"),
            reject("@e", "code_nobody_heard_of"),
        ],
        reject_counts={"user": 1},
        temporary_rejects=[],
        retry_offers={},
    )

    body = owner.get("/database").text

    assert "<form" not in body.lower() and "<button" not in body.lower()
    assert "постоянный отказ" in body
    assert "мы уже в этом чате" in body
    assert "ждём модератора" in body
    assert body.count("неизвестно: не различить отсутствие чата и сбой") == 2


def test_the_page_offers_no_select_all_and_no_bulk_route(owner: TestClient, calls: Calls) -> None:
    body = owner.get("/database").text
    routes = [
        (getattr(route, "path", ""), sorted(getattr(route, "methods", None) or []))
        for route in dashboard_app.create_app().routes
        if "reject" in getattr(route, "path", "")
    ]

    assert "checkbox" not in body.lower() and "выбрать все" not in body.lower()
    assert routes == [("/rejects/{key}/retry", ["POST"])]


@pytest.mark.parametrize(
    ("tracked", "cap", "expected", "forbidden"),
    [
        (12, 200, "встанет в очередь; сейчас 12 из 200 чатов", "вступлений не будет"),
        (199, 200, "сейчас 199 из 200 чатов", "вступлений не будет"),
        (200, 200, "вступлений не будет, пока лимит заполнен", "встанет в очередь"),
        # потолок меняли с 50 до 200: число берётся из данных, а не из шаблона
        (7, 7, "7 из 7 чатов: лимит заполнен", "из 200"),
    ],
)
def test_the_cap_warning_uses_the_real_count_and_cap(
    owner: TestClient, calls: Calls, tracked: int, cap: int, expected: str, forbidden: str
) -> None:
    calls.view = inventory(tracked_chats=tracked, chat_cap=cap)

    body = owner.get("/database").text

    assert expected in body
    assert forbidden not in body


def test_the_default_cap_is_the_real_constant_not_a_copy() -> None:
    from sniffer.sources.telegram_discover_reference import MAX_TRACKED_CHATS

    assert data.Inventory().chat_cap == MAX_TRACKED_CHATS


def test_the_journal_shows_the_state_and_the_original_reject(
    owner: TestClient, calls: Calls
) -> None:
    calls.view = inventory(
        retries=[
            RetryRecord(
                id=1,
                reject_key=NASTY,
                reject_reason=TEMP,
                reject_rejected_at=WHEN,
                requested_at=WHEN,
                requested_by=OWNER,
                status="done",
                outcome="rejected_again",
                next_retry_at=WHEN + timedelta(days=1),
            )
        ],
        retries_today=3,
    )

    body = owner.get("/database").text

    assert "отклонён снова" in body
    assert "3 из 10" in body
    assert NASTY not in body and "&lt;script&gt;" in body


def test_an_unknown_note_value_in_the_query_is_never_echoed(
    owner: TestClient, calls: Calls
) -> None:
    body = owner.get("/database?retry=<script>alert(1)</script>").text

    assert "<script>alert(1)" not in body
    assert "Поставлено в очередь" not in body
    assert "Поставлено в очередь" in owner.get("/database?retry=created").text


# ── доступ и CSRF ───────────────────────────────────────────────────────────


def test_a_stranger_cannot_post_and_the_database_is_never_asked(
    client: TestClient, calls: Calls
) -> None:
    response = post(client)

    assert response.status_code == 401
    assert "telegram-widget.js" in response.text
    assert calls.retries == []


def test_a_forged_session_cookie_is_a_stranger(client: TestClient, calls: Calls) -> None:
    client.cookies.set(auth.COOKIE_NAME, f"owner:{OWNER}:9999999999.deadbeef")

    assert post(client).status_code == 401
    assert calls.retries == []


@pytest.mark.parametrize("csrf", ["", "garbage", "csrf:1:2.deadbeef", "no-dot"])
def test_a_post_without_a_valid_csrf_token_is_forbidden(
    owner: TestClient, calls: Calls, csrf: str
) -> None:
    response = post(owner, csrf=csrf)

    assert response.status_code == 403
    assert "форма устарела" in response.text
    assert calls.retries == []


def test_the_session_cookie_is_not_a_csrf_token(owner: TestClient, calls: Calls) -> None:
    """Префиксы разделяют назначения подписи: cookie не подходит как токен формы."""
    cookie, _ = auth.issue_session()

    assert post(owner, csrf=cookie).status_code == 403
    assert calls.retries == []


def test_the_csrf_token_of_another_owner_is_forbidden(
    owner: TestClient, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = auth.issue_csrf()
    monkeypatch.setenv("OWNER_CHAT_ID", "42")
    from sniffer.config import reload_settings

    reload_settings()

    assert post(owner, csrf=token).status_code in (401, 403)
    assert calls.retries == []


def test_a_get_to_the_retry_route_does_nothing(owner: TestClient, calls: Calls) -> None:
    assert owner.get("/rejects/%40t_open/retry").status_code == 405
    assert calls.retries == []


# ── успех, идемпотентность, отказы ──────────────────────────────────────────


def test_a_valid_post_retries_exactly_that_key_and_redirects_back(
    owner: TestClient, calls: Calls
) -> None:
    response = post(owner, "%40t_open", request_id="abcdef0123456789")

    assert response.status_code == 303
    assert response.headers["location"] == "/database?retry=created"
    assert calls.retries == [
        {"key": "@t_open", "idempotency_key": "abcdef0123456789", "requested_by": OWNER}
    ]


def test_an_invite_key_survives_the_url(owner: TestClient, calls: Calls) -> None:
    """`+` в пути — буква, а не пробел; в форме он уходит как %2B."""
    post(owner, "%2BInv_ite-1")

    assert calls.retries[0]["key"] == "+Inv_ite-1"


def test_a_replayed_form_is_told_so_and_creates_nothing_new(
    owner: TestClient, calls: Calls
) -> None:
    calls.result = RetryResult(RetryStatus.REPLAYED, OK)

    response = post(owner)

    assert response.status_code == 303 and response.headers["location"].endswith("retry=replayed")
    assert "вторая попытка не заведена" in owner.get("/database?retry=replayed").text


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (RetryCode.PERMANENT, 409),
        (RetryCode.MEMBERSHIP, 409),
        (RetryCode.PENDING, 409),
        (RetryCode.UNKNOWN, 409),
        (RetryCode.ATTEMPTS_EXHAUSTED, 409),
        (RetryCode.COOLDOWN, 409),
        (RetryCode.FLOOD_STOP, 409),
        (RetryCode.ALREADY_QUEUED, 409),
        (RetryCode.DAILY_LIMIT, 429),
        (RetryCode.NOT_FOUND, 404),
        (RetryCode.BAD_KEY, 400),
    ],
)
def test_a_refusal_has_its_own_status_and_plain_words(
    owner: TestClient, calls: Calls, code: RetryCode, status: int
) -> None:
    calls.result = RetryResult(RetryStatus.REFUSED, RetryDecision(code, f"причина: {code.value}"))

    response = post(owner)

    assert response.status_code == status
    assert f"причина: {code.value}" in response.text
    assert (
        "Content-Security-Policy" in response.headers
        or "content-security-policy" in response.headers
    )


def test_a_daily_limit_refusal_says_when_and_sends_retry_after(
    owner: TestClient, calls: Calls
) -> None:
    until = datetime.now(UTC) + timedelta(hours=3)
    calls.result = RetryResult(
        RetryStatus.REFUSED,
        RetryDecision(RetryCode.DAILY_LIMIT, "лимит повторов исчерпан", available_at=until),
    )

    response = post(owner)

    assert response.status_code == 429
    assert 3 * 3600 - 120 <= int(response.headers["retry-after"]) <= 3 * 3600
    assert "Доступно с" in response.text


def test_the_limits_are_not_a_dashboard_copy() -> None:
    assert reject_retry.MAX_RETRIES_PER_DAY == 10  # единственное место, где живёт число


# ── охрана ──────────────────────────────────────────────────────────────────


def test_an_exception_of_a_type_nobody_listed_is_a_generic_500_without_details(
    owner: TestClient, calls: Calls
) -> None:
    """Охрана до корня: ответ не зависит от того, знаем ли мы класс исключения."""
    calls.raises = Boom("password=hunter2 host=10.0.0.5")

    response = post(owner)

    assert response.status_code == 500
    assert "Внутренняя ошибка" in response.text
    assert "hunter2" not in response.text and "10.0.0.5" not in response.text


def test_every_action_leaves_a_log_event_with_the_key_and_the_outcome(
    owner: TestClient, calls: Calls
) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        post(owner)
        post(owner, csrf="")
        calls.result = RetryResult(
            RetryStatus.REFUSED, RetryDecision(RetryCode.COOLDOWN, "ещё рано")
        )
        post(owner)

    events = [(entry["event"], entry.get("code") or entry.get("reason")) for entry in logs]
    assert ("dashboard.reject_retry", "ok") in events
    assert ("dashboard.reject_retry_denied", "csrf") in events
    assert ("dashboard.reject_retry", "cooldown") in events
