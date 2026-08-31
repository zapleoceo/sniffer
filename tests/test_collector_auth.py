"""Подкоманда `python -m sniffer.collector auth`.

Главная проверка здесь — не «работает ли вход», а **куда девается строка
сессии**. Строка равносильна паролю от аккаунта: попав в structlog, она уедет
в `docker logs`, оттуда в сборщик логов и переживёт любую ротацию секретов.
Поэтому у неё ровно один выход — stdout, и ровно один раз.

Живой сети тут нет: Telethon-клиент подменён фейком, который записывает,
какие методы дёрнули. Заодно это доказывает второе требование — юзербот
ничего не отправляет, кроме самого запроса кода.
"""

from __future__ import annotations

import logging

import pytest
from structlog.testing import capture_logs
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from sniffer.collector import __main__ as collector_main
from sniffer.collector.auth import (
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_TELEGRAM_REFUSED,
    Console,
    run_auth,
)
from sniffer.config import Settings

SESSION = "1BQANOTEuMTA4LjU2LjEyOQG7fake-session-string"
PHONE = "+84900000000"

# Методы, которые команда вправе дёрнуть у Telegram. Всё остальное —
# исходящее действие, а юзербот только читает (spec-v2, 6.1).
ALLOWED_CALLS = {"connect", "send_code_request", "sign_in", "disconnect"}


class FakeSession:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


class FakeClient:
    """Записывает вызовы: тест смотрит не только на результат, но и на путь."""

    def __init__(self, *, needs_password: bool = False, refuses: Exception | None = None) -> None:
        self.session = FakeSession(SESSION)
        self.calls: list[str] = []
        self.sign_in_args: list[tuple[str | None, str | None, str | None]] = []
        self._needs_password = needs_password
        self._refuses = refuses

    async def connect(self) -> None:
        self.calls.append("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def send_code_request(self, phone: str) -> object:
        self.calls.append("send_code_request")
        assert phone == PHONE
        if self._refuses is not None:
            raise self._refuses
        return object()

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        *,
        password: str | None = None,
    ) -> object:
        self.calls.append("sign_in")
        self.sign_in_args.append((phone, code, password))
        if self._needs_password and password is None:
            raise SessionPasswordNeededError(request=None)
        return object()


class Recorder:
    """Консоль без терминала: помнит подсказки и отвечает заготовками."""

    def __init__(self, *, code: str = "12345", password: str = "секрет") -> None:  # noqa: S107
        self.said: list[str] = []
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self._code = code
        self._password = password

    @property
    def console(self) -> Console:
        return Console(say=self.said.append, ask=self._ask, ask_secret=self._ask_secret)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._code

    def _ask_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return self._password


def _ready_settings() -> Settings:
    # _env_file=None: результат теста не должен зависеть от .env на машине.
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        tg_api_id=42,
        tg_api_hash="hash",
        tg_phone=PHONE,
    )


def _blank_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_auth_prints_the_session_exactly_once(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()
    recorder = Recorder(code="54321")

    code = run_auth(_ready_settings(), recorder.console, client_factory=lambda _id, _hash: client)

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert captured.out.strip() == SESSION, "в stdout не должно быть ничего, кроме сессии"
    assert captured.out.count(SESSION) == 1
    assert client.sign_in_args == [(PHONE, "54321", None)]
    assert set(client.calls) <= ALLOWED_CALLS, "юзербот только читает: лишних вызовов быть не может"
    assert client.calls[-1] == "disconnect", "соединение обязано закрыться"


def test_code_prompt_says_the_code_arrives_in_telegram() -> None:
    """Без этой подсказки владелец ждёт SMS, которой Telegram не пришлёт."""
    recorder = Recorder()

    run_auth(_ready_settings(), recorder.console, client_factory=lambda _id, _hash: FakeClient())

    assert recorder.prompts, "код обязаны спросить"
    assert "Telegram" in recorder.prompts[0]
    assert "SMS" in recorder.prompts[0]


def test_auth_without_settings_names_them_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()

    def _must_not_connect(_id: int, _hash: str) -> FakeClient:
        raise AssertionError("без настроек в Telegram ходить незачем")

    code = run_auth(_blank_settings(), recorder.console, client_factory=_must_not_connect)

    assert code == EXIT_NOT_CONFIGURED, "тихий выход нулём выглядит как успех"
    complaint = "\n".join(recorder.said)
    assert "TG_API_ID" in complaint
    assert "TG_API_HASH" in complaint
    assert "TG_PHONE" in complaint
    assert capsys.readouterr().out == "", "в stdout уходит только сессия"


def test_auth_asks_for_the_two_factor_password(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient(needs_password=True)
    recorder = Recorder(password="облачный-пароль")

    code = run_auth(_ready_settings(), recorder.console, client_factory=lambda _id, _hash: client)

    assert code == EXIT_OK
    assert recorder.secret_prompts, "аккаунт с 2FA обязан спросить пароль, а не упасть"
    assert client.sign_in_args[-1] == (None, None, "облачный-пароль")
    assert capsys.readouterr().out.strip() == SESSION


def test_telegram_refusal_is_a_message_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Владельцу нужно «подождите N секунд», а не стек вызовов."""
    client = FakeClient(refuses=FloodWaitError(request=None, capture=30))
    recorder = Recorder()

    code = run_auth(_ready_settings(), recorder.console, client_factory=lambda _id, _hash: client)

    assert code == EXIT_TELEGRAM_REFUSED
    assert any("FloodWaitError" in line for line in recorder.said)
    assert capsys.readouterr().out == "", "сессии нет — печатать нечего"
    assert client.calls[-1] == "disconnect", "соединение закрывается и на ошибке"


def test_session_never_reaches_any_log(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Строка сессии = пароль от аккаунта. В логе она живёт вечно, поэтому её там нет."""
    client = FakeClient()
    recorder = Recorder()

    with caplog.at_level(logging.DEBUG), capture_logs() as events:
        run_auth(_ready_settings(), recorder.console, client_factory=lambda _id, _hash: client)

    captured = capsys.readouterr()
    assert SESSION not in caplog.text, "сессия утекла в stdlib-логи"
    assert all(SESSION not in repr(event) for event in events), "сессия утекла в structlog"
    assert SESSION not in captured.err, "stderr читают через docker logs так же, как stdout"
    assert SESSION not in "\n".join(recorder.said), "сессия попала в подсказки"
    assert captured.out.count(SESSION) == 1, "единственный выход строки — stdout, один раз"


def test_plain_start_is_untouched_by_the_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Аргумент `auth` — единственное отличие; без него процесс работает как раньше."""
    started: list[str] = []
    monkeypatch.setattr(collector_main, "run_service", lambda service: started.append(service.name))
    monkeypatch.setattr(collector_main, "run_auth", lambda: 7)

    assert collector_main.main([]) == EXIT_OK
    assert started == ["collector"]

    assert collector_main.main(["auth"]) == 7
    assert started == ["collector"], "подкоманда не поднимает обычный процесс"
