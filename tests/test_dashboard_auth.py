"""Вход в интерфейс владельца: что именно закрыто и чем.

Страница показывает переписку клиентов и расходы, поэтому проверяется не
«работает ли вход», а каждая атака отдельно: подделка cookie, повтор параметров
виджета, чужой аккаунт, просроченная подпись, смена владельца.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from sniffer.config import get_settings, reload_settings
from sniffer.dashboard import auth
from tests.conftest import DashboardEnv


def test_valid_widget_signature_gives_the_user_id(dashboard_env: DashboardEnv) -> None:
    assert auth.authorize_widget(dashboard_env.sign()) == dashboard_env.owner


def test_tampered_signature_is_rejected(dashboard_env: DashboardEnv) -> None:
    data = dashboard_env.sign()
    # Меняем один символ подписи: HMAC обязан не сойтись.
    data["hash"] = ("0" if data["hash"][0] != "0" else "1") + data["hash"][1:]

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(data)

    assert failure.value.status == 403


def test_tampered_payload_is_rejected(dashboard_env: DashboardEnv) -> None:
    """Подменить id, оставив чужую подпись, не выйдет."""
    data = dashboard_env.sign()
    data["id"] = str(dashboard_env.stranger)

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(data)

    assert failure.value.status == 403


def test_signature_made_with_another_bot_token_is_rejected(dashboard_env: DashboardEnv) -> None:
    with pytest.raises(auth.AuthError):
        auth.authorize_widget(dashboard_env.sign(token="999:чужой-бот"))


def test_stale_auth_date_is_rejected(dashboard_env: DashboardEnv) -> None:
    """Иначе перехваченный однажды набор параметров работает почти сутки."""
    stale = int(time.time()) - auth.WIDGET_AUTH_TTL_S - 5

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(dashboard_env.sign(auth_date=stale))

    assert failure.value.status == 403
    assert "просроч" in failure.value.message


def test_auth_date_from_the_future_is_rejected(dashboard_env: DashboardEnv) -> None:
    future = int(time.time()) + auth.CLOCK_SKEW_S + 120

    with pytest.raises(auth.AuthError):
        auth.authorize_widget(dashboard_env.sign(auth_date=future))


def test_same_widget_response_works_only_once(dashboard_env: DashboardEnv) -> None:
    """Внутри окна свежести повтор тоже не проходит."""
    data = dashboard_env.sign()
    assert auth.authorize_widget(data) == dashboard_env.owner

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(data)

    assert "использована" in failure.value.message


def test_foreign_account_gets_403_not_an_empty_page(dashboard_env: DashboardEnv) -> None:
    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(dashboard_env.sign(dashboard_env.stranger))

    assert failure.value.status == 403
    assert "не владелец" in failure.value.message


@pytest.mark.parametrize("bad_hash", ["ы" * 64, "не-подпись", "", "ab", "z" * 64, "ЁЁ" * 32])
def test_malformed_hash_is_refused_not_crashed(dashboard_env: DashboardEnv, bad_hash: str) -> None:
    """`compare_digest` на не-ASCII бросает TypeError — это было бы 500 вместо 403."""
    data = dashboard_env.sign()
    data["hash"] = bad_hash

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(data)

    assert failure.value.status == 400


@pytest.mark.parametrize("bad_cookie", ["owner:1:1.ы" * 1, "owner:1:1." + "ы" * 64, "owner:1:1."])
def test_malformed_cookie_signature_is_refused_not_crashed(
    dashboard_env: DashboardEnv, bad_cookie: str
) -> None:
    assert not auth.is_owner_session(bad_cookie)
    assert not auth.is_valid_csrf(bad_cookie)


def test_missing_hash_is_a_bad_request(dashboard_env: DashboardEnv) -> None:
    data = dashboard_env.sign()
    del data["hash"]

    with pytest.raises(auth.AuthError) as failure:
        auth.authorize_widget(data)

    assert failure.value.status == 400


def test_session_cookie_round_trips(dashboard_env: DashboardEnv) -> None:
    token, ttl = auth.issue_session()

    assert auth.is_owner_session(token)
    assert ttl == auth.SESSION_TTL_S


def test_forged_session_cookie_is_rejected(dashboard_env: DashboardEnv) -> None:
    token, _ = auth.issue_session()
    body, signature = token.rsplit(".", 1)

    assert not auth.is_owner_session(f"{body}.{'0' * len(signature)}")
    assert not auth.is_owner_session(body)
    assert not auth.is_owner_session(f"owner:{dashboard_env.owner}:{int(time.time())}")
    assert not auth.is_owner_session(None)


def test_session_cookie_cannot_be_signed_with_the_bot_token(dashboard_env: DashboardEnv) -> None:
    """Виджет и cookie подписаны разными секретами — это проверяется здесь."""
    body = f"owner:{dashboard_env.owner}:{int(time.time())}"
    forged = hmac.new(dashboard_env.bot_token.encode(), body.encode(), hashlib.sha256).hexdigest()

    assert not auth.is_owner_session(f"{body}.{forged}")


def test_session_survives_process_restart(dashboard_env: DashboardEnv) -> None:
    """Cookie подписана, а не хранится в памяти: рестарт её не роняет.

    Сброс кэша настроек и очистка памяти процесса — ровно то, что видит новый
    процесс с тем же `.env`.
    """
    token, _ = auth.issue_session()

    reload_settings()
    auth.forget_used_widget_hashes()

    assert auth.is_owner_session(token)


def test_expired_session_cookie_is_rejected(dashboard_env: DashboardEnv) -> None:
    stale = auth._sign("owner", now=time.time() - auth.SESSION_TTL_S - 10)

    assert not auth.is_owner_session(stale)


def test_changing_the_owner_invalidates_old_cookies(
    dashboard_env: DashboardEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смена владельца обесценивает выданные cookie без списка отзыва."""
    token, _ = auth.issue_session()
    monkeypatch.setenv("OWNER_CHAT_ID", str(dashboard_env.stranger))
    reload_settings()

    assert not auth.is_owner_session(token)


def test_without_owner_nobody_gets_in(
    dashboard_env: DashboardEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OWNER_CHAT_ID", "")
    reload_settings()
    assert "OWNER_CHAT_ID" in auth.missing_settings()

    with pytest.raises(auth.AuthError):
        auth.authorize_widget(dashboard_env.sign())


def test_without_session_secret_login_is_off(
    dashboard_env: DashboardEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой секрет подписал бы всё и проверил бы всё — это открытая дверь."""
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "")
    reload_settings()

    with pytest.raises(auth.AuthError) as failure:
        auth.issue_session()

    assert failure.value.status == 503
    assert not auth.is_owner_session("owner:1:1.abc")


def test_csrf_token_is_not_interchangeable_with_the_session(dashboard_env: DashboardEnv) -> None:
    """Разные префиксы: токен формы не годится как cookie и наоборот."""
    session, _ = auth.issue_session()
    csrf = auth.issue_csrf()

    assert auth.is_valid_csrf(csrf)
    assert not auth.is_valid_csrf(session)
    assert not auth.is_owner_session(csrf)


def test_expired_csrf_token_is_rejected(dashboard_env: DashboardEnv) -> None:
    stale = auth._sign("csrf", now=time.time() - auth.CSRF_TTL_S - 10)

    assert not auth.is_valid_csrf(stale)


def test_bot_token_is_never_the_session_secret(dashboard_env: DashboardEnv) -> None:
    """Один ключ на две задачи — утечка одной обесценивает обе."""
    settings = get_settings()

    assert settings.bot_token != settings.dashboard_session_secret
    assert settings.bot_token != settings.secret_encryption_key
