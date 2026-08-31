"""Переавторизация юзербота — без Telegram и без базы.

Проверяется то, из-за чего этот код опасен: секрет проходит через HTTP. Значит,
интересны не удачный путь (его закрывает только живой Telegram), а границы —
истёкший поток, незаполненное окружение, пустой ввод и то, что строка сессии
уходит в шифрование, а не в ответ.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from telethon.errors import SessionPasswordNeededError

from sniffer.config import reload_settings
from sniffer.dashboard import data, reauth

SESSION_STRING = "1BQANOTEuMTA4LjU2LjEyOAG7VERYSECRETSESSION"


class FakeClient:
    """Ровно то, что поток трогает у Telethon-клиента."""

    def __init__(self, *, needs_password: bool = False, fails: bool = False) -> None:
        self.needs_password = needs_password
        self.fails = fails
        self.disconnected = False
        self.session = object()
        self.signed_in_with: list[tuple[str, str]] = []

    async def sign_in(self, *args: Any, **kwargs: Any) -> None:
        if "password" in kwargs:
            self.signed_in_with.append(("password", kwargs["password"]))
            return
        self.signed_in_with.append(("code", args[1] if len(args) > 1 else ""))
        if self.needs_password:
            raise SessionPasswordNeededError(request=None)
        if self.fails:
            raise RuntimeError("PHONE_CODE_INVALID")

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture(autouse=True)
def clean_flows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_PHONE", "+84900000000")
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "encryption-key-длинный-и-случайный-32+")
    reload_settings()
    reauth._flows.clear()
    yield
    reauth._flows.clear()
    reload_settings()


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Перехватываем сохранение сессии: настоящее пошло бы в Postgres."""
    calls: list[tuple[str, str]] = []

    async def save_session(phone: str, session_string: str) -> None:
        calls.append((phone, session_string))

    monkeypatch.setattr(data, "save_session", save_session)
    return calls


def _flow(client: FakeClient, *, needs_password: bool = False) -> str:
    reauth._flows["flow-1"] = reauth.Flow(
        client=client,
        phone="+84900000000",
        code_hash="hash",
        needs_password=needs_password,
    )
    return "flow-1"


async def test_unknown_flow_is_refused_not_crashed() -> None:
    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.verify("нет-такого", code="12345")

    assert "истёк" in str(failure.value)


async def test_empty_code_is_refused_before_telegram() -> None:
    client = FakeClient()
    flow_id = _flow(client)

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.verify(flow_id, code="   ")

    assert "введите код" in str(failure.value)
    assert client.signed_in_with == [], "в Telegram не пошли — и не должны были"


async def test_empty_password_is_refused_before_telegram() -> None:
    client = FakeClient()
    flow_id = _flow(client, needs_password=True)

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.verify(flow_id, password="")

    assert "облачный пароль" in str(failure.value)
    assert client.signed_in_with == []


async def test_cloud_password_switches_the_flow_to_the_second_step() -> None:
    client = FakeClient(needs_password=True)
    flow_id = _flow(client)

    with pytest.raises(reauth.NeedsPassword):
        await reauth.verify(flow_id, code="12345")

    assert reauth.needs_password(flow_id), "второй шаг обязан помниться"
    assert flow_id in reauth._flows, "поток нельзя закрывать между шагами"


async def test_session_string_is_saved_and_not_returned(
    saved: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Строка сессии уходит в хранилище, а наружу — только номер."""
    monkeypatch.setattr(reauth, "session_string_of", lambda _client: SESSION_STRING)
    client = FakeClient()
    flow_id = _flow(client)

    returned = await reauth.verify(flow_id, code="12345")

    assert returned == "+84900000000"
    assert SESSION_STRING not in returned
    assert saved == [("+84900000000", SESSION_STRING)]
    assert client.disconnected, "соединение с Telegram обязано закрыться"
    assert flow_id not in reauth._flows, "завершённый поток не должен жить дальше"


async def test_failed_sign_in_keeps_the_flow_for_a_retry() -> None:
    client = FakeClient(fails=True)
    flow_id = _flow(client)

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.verify(flow_id, code="00000")

    assert "PHONE_CODE_INVALID" in str(failure.value)
    assert flow_id in reauth._flows, "код можно ввести заново, не начиная сначала"


async def test_telegram_error_text_is_truncated() -> None:
    """Внутренняя кухня Telethon владельцу не нужна, а в HTML — тем более."""
    client = FakeClient()
    flow_id = _flow(client)

    async def sign_in(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("э" * 5000)

    client.sign_in = sign_in  # type: ignore[method-assign]

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.verify(flow_id, code="12345")

    assert len(str(failure.value)) < reauth.MAX_ERROR_CHARS + 100


async def test_stale_flow_is_closed_not_leaked() -> None:
    """Каждый брошенный поток держит открытое соединение с Telegram."""
    client = FakeClient()
    reauth._flows["old"] = reauth.Flow(
        client=client,
        phone="+84900000000",
        code_hash="hash",
        touched_at=time.monotonic() - reauth.FLOW_TTL_S - 10,
    )

    await reauth.prune()

    assert "old" not in reauth._flows
    assert client.disconnected


async def test_missing_telegram_settings_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "")
    monkeypatch.setenv("TG_API_HASH", "")
    reload_settings()

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.start("+84900000000")

    assert "TG_API_ID" in str(failure.value)
    assert "TG_API_HASH" in str(failure.value)


async def test_no_phone_anywhere_is_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_PHONE", "")
    reload_settings()

    with pytest.raises(reauth.ReauthError) as failure:
        await reauth.start("   ")

    assert "номер" in str(failure.value)


class SlowClient(FakeClient):
    """Клиент, который отпускает управление внутри `sign_in`.

    Так воспроизводится двойной клик: пока первый запрос ждёт Telegram, второй
    успевает войти в тот же поток. Настоящий Telethon ждёт сеть — здесь хватает
    `sleep(0)`, чтобы петля отдала управление второму запросу.
    """

    async def sign_in(self, *args: Any, **kwargs: Any) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        if self.disconnected:
            # Ровно то, чем оборачивался баг: операция на отключённом клиенте.
            raise RuntimeError("Cannot send requests while disconnected")
        await super().sign_in(*args, **kwargs)


async def test_double_click_does_not_sign_in_twice(
    monkeypatch: pytest.MonkeyPatch, saved: list[tuple[str, str]]
) -> None:
    """Два одновременных POST с одним flow_id: вход один, ответ второму внятный.

    Без лока оба запроса читали поток до того, как первый сделает `disconnect()`
    и снимет его с учёта, и второй падал ошибкой Telethon об отключённом
    клиенте вместо «поток истёк — начните заново».
    """
    client = SlowClient()
    monkeypatch.setattr(reauth, "session_string_of", lambda _: SESSION_STRING)
    flow_id = _flow(client)

    results = await asyncio.gather(
        reauth.verify(flow_id, code="12345"),
        reauth.verify(flow_id, code="12345"),
        return_exceptions=True,
    )

    ok = [item for item in results if isinstance(item, str)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(ok) == 1, f"вход обязан пройти ровно один раз, получено {results}"
    assert len(failures) == 1
    assert isinstance(failures[0], reauth.ReauthError)
    assert "поток истёк" in str(failures[0]), f"второму нужен внятный ответ, а не {failures[0]}"
    assert len(client.signed_in_with) == 1, "код нельзя предъявлять Telegram дважды"
    assert len(saved) == 1, "сессия сохраняется один раз"


async def test_busy_flow_survives_a_concurrent_prune() -> None:
    """`prune` не отключает клиент из-под работающего шага.

    Шаг может идти дольше TTL (Telegram отвечает не мгновенно). Сборка мусора,
    запущенная параллельным запросом, не должна выдёргивать у него соединение.
    """
    client = SlowClient()
    flow = reauth.Flow(
        client=client,
        phone="+84900000000",
        code_hash="hash",
        touched_at=time.monotonic() - reauth.FLOW_TTL_S - 10,
    )
    reauth._flows["busy"] = flow

    async with flow.lock:
        await reauth.prune()

    assert "busy" in reauth._flows, "занятый поток не выбрасываем"
    assert not client.disconnected, "соединение работающего шага не рвём"
