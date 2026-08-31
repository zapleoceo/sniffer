"""Страницы интерфейса по HTTP: доступ, экранирование, CSRF, гигиена секретов.

База подменена: проверяется поведение веб-слоя, а работу репозиториев проверяют
`test_db_repositories.py`. Подмена именно на границе `dashboard/data.py` — ниже
её начинается SQL, и подделывать его значило бы проверять подделку.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sniffer.config import reload_settings
from sniffer.dashboard import app as dashboard_app
from sniffer.dashboard import auth, data, reauth, views
from sniffer.domain.records import (
    REQUEST_DONE,
    BrokerCall,
    ClientRequest,
    DialogMessage,
    SessionState,
    User,
)
from tests.conftest import BOT_TOKEN, OWNER, SESSION_SECRET, STRANGER, DashboardEnv

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

# Текст объявления или клиента может содержать что угодно: он приезжает из
# чужих чатов. Именно эта строка не должна стать разметкой.
NASTY = '<script>alert("xss")</script> "кавычки" & <img src=x onerror=1>'

USER = User(id=7, tg_user_id=OWNER, username=NASTY, lang="ru", created_at=NOW)
REQUEST = ClientRequest(
    id=3,
    user_id=7,
    raw_query=NASTY,
    status=REQUEST_DONE,
    stages={"intake_ms": 1200, "plan_ms": 900, "search_ms": 8000},
    plan_fallback=True,
    sources=["chotot"],
    result_count=5,
    started_at=NOW,
    finished_at=NOW,
    duration_ms=10100,
)
CALL = BrokerCall(
    capability="structured",
    request_id=3,
    broker_request_id=4242,
    provider="groq",
    model="llama-3.3-70b",
    tokens_in=120,
    tokens_out=45,
    cost_usd=Decimal("0.000123"),
    latency_ms=870,
    created_at=NOW,
    id=1,
)
DIALOG = [
    DialogMessage(id=1, user_id=7, direction="in", text=NASTY, request_id=3, created_at=NOW),
    DialogMessage(
        id=2, user_id=7, direction="out", text="Понял, ищу.", request_id=3, created_at=NOW
    ),
]
STATE = SessionState(id=1, phone="+84900000000", is_active=True, last_ok_at=NOW)


@pytest.fixture(autouse=True)
def env(dashboard_env: DashboardEnv) -> DashboardEnv:
    """Окружение владельца на каждый тест этого файла."""
    return dashboard_env


@pytest.fixture(autouse=True)
def fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Данные вместо базы. Дашборд читает только через `data.py`."""
    overview = data.Overview(
        stats={
            "users": 1,
            "chats": 12,
            "chats_active": 9,
            "raw_messages": 340,
            "listings": 40,
            "listings_active": 38,
            "listings_fresh": 21,
        },
        request_totals={"requests": 4, "failed": 1, "fallbacks": 2, "avg_duration_ms": 9000},
        cost_totals={"calls": 6, "tokens_in": 700, "tokens_out": 210, "cost_usd": Decimal("0.004")},
        users=[USER],
        requests_by_user={7: 4},
        requests=[data.RequestRow(request=REQUEST, tokens=165, cost_usd=Decimal("0.000123"))],
        session=STATE,
    )

    async def fake_overview() -> data.Overview:
        return overview

    async def fake_request_detail(request_id: int) -> data.RequestDetail | None:
        if request_id != REQUEST.id:
            return None
        return data.RequestDetail(
            row=data.RequestRow(request=REQUEST, tokens=165, cost_usd=Decimal("0.000123")),
            user=USER,
            dialog=DIALOG,
            calls=[CALL],
        )

    async def fake_user_detail(user_id: int) -> data.UserDetail | None:
        return data.UserDetail(user=USER, dialog=DIALOG) if user_id == USER.id else None

    async def fake_session_states() -> list[SessionState]:
        return [STATE]

    for name, value in (
        ("overview", fake_overview),
        ("request_detail", fake_request_detail),
        ("user_detail", fake_user_detail),
        ("session_states", fake_session_states),
    ):
        monkeypatch.setattr(data, name, value)


@pytest.fixture
def client() -> Iterator[TestClient]:
    # raise_server_exceptions=False: обработчик 500 обязан отвечать страницей, и
    # проверить это можно только если клиент не поднимает исключение сам.
    with TestClient(dashboard_app.create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def owner(client: TestClient) -> TestClient:
    token, _ = auth.issue_session()
    client.cookies.set(auth.COOKIE_NAME, token)
    return client


# ── доступ ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/requests/3", "/users/7", "/session"])
def test_stranger_sees_no_data(client: TestClient, path: str) -> None:
    """Неавторизованный не видит ни строки: только форму входа."""
    response = client.get(path)

    assert response.status_code == 401
    assert "telegram-widget.js" in response.text
    for secret in ("скутер", "structured", "groq", "4242", "+84900000000"):
        assert secret not in response.text


def test_forged_cookie_sees_no_data(client: TestClient) -> None:
    client.cookies.set(auth.COOKIE_NAME, f"owner:{OWNER}:9999999999.deadbeef")

    assert client.get("/").status_code == 401


def test_owner_sees_the_overview(owner: TestClient) -> None:
    response = owner.get("/")

    assert response.status_code == 200
    assert "Запросы" in response.text
    assert "Клиенты" in response.text
    # Фолбэки и стоимость — те самые числа, за которыми страницу и открывают.
    assert "50%" in response.text
    assert "$0.004000" in response.text


def test_request_page_shows_cost_and_stage_timings(owner: TestClient) -> None:
    response = owner.get("/requests/3")

    assert response.status_code == 200
    # Расход связан с запросом по request_id брокера, а не по времени.
    assert "4242" in response.text
    assert "$0.000123" in response.text
    assert "intake_ms" in response.text and "1.2 с" in response.text
    assert "8.0 с" in response.text


def test_stages_are_shown_in_the_order_they_happened() -> None:
    """`jsonb` порядок ключей не хранит — порядок обязан задавать код."""
    shuffled = {"search_ms": 8000, "extract_ms": 300, "intake_ms": 1200, "plan_ms": 900}

    assert [name for name, _ in views._ordered_stages(shuffled)] == [
        "intake_ms",
        "plan_ms",
        "search_ms",
        # Незнакомый этап — после известных, а не молча первым по алфавиту.
        "extract_ms",
    ]


def test_unknown_request_is_404(owner: TestClient) -> None:
    assert owner.get("/requests/999").status_code == 404


def test_unknown_user_is_404(owner: TestClient) -> None:
    assert owner.get("/users/999").status_code == 404


def test_healthz_needs_no_login_and_leaks_nothing(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "missing": []}


def test_healthz_names_what_is_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "")
    reload_settings()

    assert client.get("/healthz").json()["missing"] == ["DASHBOARD_SESSION_SECRET"]


# ── вход через виджет ────────────────────────────────────────────────────────


def test_widget_callback_sets_a_hardened_cookie(client: TestClient, env: DashboardEnv) -> None:
    response = client.get("/auth/telegram", params=env.sign(), follow_redirects=False)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()


def test_widget_callback_rejects_a_foreign_account(client: TestClient, env: DashboardEnv) -> None:
    response = client.get("/auth/telegram", params=env.sign(STRANGER))

    assert response.status_code == 403
    assert "не владелец" in response.text


def test_widget_callback_without_signature_is_refused(client: TestClient) -> None:
    assert client.get("/auth/telegram", params={"id": str(OWNER)}).status_code == 400


@pytest.mark.parametrize(
    "bad_cookie",
    [
        "owner:1:1.",
        "owner:1:1.zz",
        "garbage-without-a-dot",
        "owner:169510539:1",
        "csrf:169510539:1." + "a" * 64,
    ],
)
def test_malformed_cookie_is_401_not_500(client: TestClient, bad_cookie: str) -> None:
    """Кривая cookie — это 401, а не 500: 500 в логе выглядел бы как поломка.

    Не-ASCII подпись здесь не проверить — HTTP-клиент сам откажется её
    отправить. Тот случай ловит `test_malformed_cookie_signature_is_refused_not_crashed`
    напрямую, потому что через сырой сокет он достижим.
    """
    client.cookies.set(auth.COOKIE_NAME, bad_cookie)

    assert client.get("/").status_code == 401


def test_logout_clears_the_cookie(owner: TestClient) -> None:
    response = owner.get("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert "sniffer_owner=" in response.headers["set-cookie"]


# ── экранирование ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/requests/3", "/users/7"])
def test_hostile_text_never_becomes_markup(owner: TestClient, path: str) -> None:
    """Текст из чужих чатов рендерится как текст, а не как разметка."""
    body = owner.get(path).text

    # Ни одного НЕэкранированного тега из враждебного текста, при том что сам
    # текст на странице есть — экранированным.
    assert "<script>" not in body
    assert "<img" not in body
    assert '"кавычки"' not in body
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in body
    assert "&lt;img src=x onerror=1&gt;" in body
    assert "&amp;" in body


# ── CSRF и переавторизация ──────────────────────────────────────────────────


def test_reauth_form_carries_a_csrf_token(owner: TestClient) -> None:
    body = owner.get("/session").text

    assert "name='csrf'" in body
    assert "csrf:" in body


def test_reauth_without_csrf_is_refused(owner: TestClient) -> None:
    response = owner.post("/session/start", data={"phone": "+84900000000"})

    assert response.status_code == 403
    assert "форма устарела" in response.text


def test_reauth_with_the_session_cookie_as_csrf_is_refused(owner: TestClient) -> None:
    """Cookie не годится как токен формы: у подписей разные префиксы."""
    session, _ = auth.issue_session()

    response = owner.post("/session/start", data={"phone": "+84900000000", "csrf": session})

    assert response.status_code == 403


def test_reauth_without_a_session_is_refused(client: TestClient) -> None:
    """Чужой сайт не отправит форму за владельца, даже угадав токен."""
    response = client.post(
        "/session/start", data={"phone": "+84900000000", "csrf": auth.issue_csrf()}
    )

    assert response.status_code == 401


def test_reauth_sends_the_code_and_asks_for_it(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def start(phone: str) -> str:
        return "flow-1"

    monkeypatch.setattr(reauth, "start", start)

    response = owner.post(
        "/session/start", data={"phone": "+84900000000", "csrf": auth.issue_csrf()}
    )

    assert response.status_code == 200
    assert "Код из Telegram" in response.text
    assert "flow-1" in response.text


def test_reauth_asks_for_the_cloud_password_when_needed(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify(flow_id: str, *, code: str = "", password: str = "") -> str:
        raise reauth.NeedsPassword

    monkeypatch.setattr(reauth, "verify", verify)
    monkeypatch.setattr(reauth, "needs_password", lambda _flow_id: True)

    response = owner.post(
        "/session/verify",
        data={"flow_id": "flow-1", "code": "12345", "csrf": auth.issue_csrf()},
    )

    assert "Облачный пароль" in response.text
    assert 'type="password"' in response.text or "type='password'" in response.text


def test_session_string_and_password_never_reach_the_page(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Секрет проходит по HTTP только внутрь; наружу не возвращается никогда."""
    secret_session = "1BQANOTEuMTA4LjU2LjEyOAG7VERYSECRETSESSION"
    cloud_password = "очень-секретный-2fa"
    seen: dict[str, str] = {}

    async def verify(flow_id: str, *, code: str = "", password: str = "") -> str:
        seen["password"] = password
        # Настоящая реализация здесь шифрует строку и кладёт в базу; наружу она
        # не возвращается — метод отдаёт только номер.
        return "+84900000000"

    monkeypatch.setattr(reauth, "verify", verify)

    response = owner.post(
        "/session/verify",
        data={
            "flow_id": "flow-1",
            "code": "12345",
            "password": cloud_password,
            "csrf": auth.issue_csrf(),
        },
    )

    assert response.status_code == 200
    assert seen["password"] == cloud_password
    assert secret_session not in response.text
    assert cloud_password not in response.text
    assert cloud_password not in caplog.text
    assert "Сессия сохранена" in response.text


def test_reauth_failure_is_shown_escaped(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def start(phone: str) -> str:
        raise reauth.ReauthError("<b>Telegram отказал</b>")

    monkeypatch.setattr(reauth, "start", start)

    response = owner.post("/session/start", data={"phone": "+84", "csrf": auth.issue_csrf()})

    assert response.status_code == 400
    assert "<b>Telegram" not in response.text
    assert "&lt;b&gt;Telegram" in response.text


# ── ошибки ──────────────────────────────────────────────────────────────────


def test_secrets_are_never_rendered(owner: TestClient) -> None:
    for path in ("/", "/session", "/requests/3"):
        body = owner.get(path).text
        assert SESSION_SECRET not in body
        assert BOT_TOKEN not in body


def test_internal_error_does_not_leak_details(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken() -> Any:
        raise RuntimeError("пароль от базы: hunter2")

    monkeypatch.setattr(data, "overview", broken)

    response = owner.get("/")

    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "Подробности в логе" in response.text


def test_openapi_and_docs_are_off(client: TestClient) -> None:
    """Ещё одна страница, которая обязана быть закрытой, однажды не будет."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ── заголовки безопасности ──────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/requests/3", "/session", "/healthz"])
def test_security_headers_are_on_every_response(owner: TestClient, path: str) -> None:
    """Второй рубеж после экранирования — и он не должен зависеть от хендлера."""
    headers = owner.get(path).headers

    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp
    # Inline-скрипт запрещён: именно там XSS и выполняется.
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"


def test_login_page_also_carries_the_headers(client: TestClient) -> None:
    """Страница входа отдаётся обработчиком исключения — её легко забыть."""
    response = client.get("/")

    assert response.status_code == 401
    assert "telegram.org" in response.headers["content-security-policy"]


def test_widget_script_source_is_allowed_by_csp(client: TestClient) -> None:
    """CSP не должна ломать сам вход: скрипт виджета обязан загружаться."""
    response = client.get("/")
    csp = response.headers["content-security-policy"]

    assert "script-src 'self' https://telegram.org" in csp
    assert "frame-src https://oauth.telegram.org" in csp
    assert "telegram.org/js/telegram-widget.js" in response.text
