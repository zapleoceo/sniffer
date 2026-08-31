"""Подкоманда `python -m sniffer.collector auth`.

Главная проверка здесь — не «работает ли вход», а **куда девается строка
сессии**. Строка равносильна паролю от аккаунта, поэтому её единственный
выход — файл с правами 0600, а в stdout уходит только путь к нему: весь вывод
контейнера забирает докеровский `json-file` и кладёт на диск общей машины.

Вторая по важности проверка — что уже полученную сессию нельзя потерять:
ни на обрыве соединения при закрытии, ни на трейсбеке.

Третья — что **ни один код возврата не приходит трейсбеком**. Каждый исход
команды описан в таблице `docs/deploy.md`, 3.5; rc=1 в той таблице значится
как «наш баг», и тесты в конце файла держат его недостижимым: диагностика не
вправе упасть ни на проверке пути, ни на выводе сообщения.

Живой сети тут нет: Telethon-клиент подменён фейком, который записывает,
какие методы дёрнули. Заодно это показывает, что юзербот ничего не отправляет.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest import mock

import pytest
import telethon.errors
from structlog.testing import capture_logs
from telethon.errors import (
    FloodWaitError,
    InvalidChecksumError,
    RPCError,
    SecurityError,
    SessionPasswordNeededError,
    TypeNotFoundError,
)

from sniffer.collector import __main__ as collector_main
from sniffer.collector import auth as collector_auth
from sniffer.collector import console as console_module
from sniffer.collector.auth import (
    EXIT_INTERRUPTED,
    EXIT_NETWORK,
    EXIT_NO_OUTPUT_FILE,
    EXIT_NO_TERMINAL,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_SIGNED_IN,
    EXIT_OK,
    EXIT_PROTOCOL,
    EXIT_TELEGRAM_REFUSED,
    EXIT_USAGE,
    run_auth,
)
from sniffer.collector.console import Console, NoTerminalError
from sniffer.collector.session_file import why_cannot_write, write_session
from sniffer.config import Settings, get_settings

SESSION = "1BQANOTEuMTA4LjU2LjEyOQG7fake-session-string"
PHONE = "+84900000000"

# Методы, которые команда вправе дёрнуть у Telegram. Всё остальное —
# исходящее действие, а юзербот только читает (spec-v2, 6.1).
ALLOWED_CALLS = {
    "connect",
    "send_code_request",
    "sign_in",
    "is_user_authorized",
    "disconnect",
}


class FakeSession:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


class SentCode:
    """То, что настоящий Telethon возвращает вместо пользователя.

    Ветка `if phone and not code and not password` в `TelegramClient.sign_in`
    молча шлёт второй запрос кода и отдаёт этот объект. Исключения нет, и
    прежний фейк — который на любой вызов отвечал успехом — эту ловушку не
    воспроизводил.
    """


class FakeClient:
    """Записывает вызовы: тест смотрит не только на результат, но и на путь."""

    def __init__(
        self,
        *,
        needs_password: bool = False,
        refuses: Exception | None = None,
        fails_to_connect: Exception | None = None,
        fails_to_disconnect: Exception | None = None,
        never_authorizes: bool = False,
    ) -> None:
        self.session = FakeSession(SESSION)
        self.calls: list[str] = []
        self.sign_in_args: list[tuple[str | None, str | None, str | None]] = []
        self._needs_password = needs_password
        self._refuses = refuses
        self._fails_to_connect = fails_to_connect
        self._fails_to_disconnect = fails_to_disconnect
        self._never_authorizes = never_authorizes
        self._authorized = False

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._fails_to_connect is not None:
            raise self._fails_to_connect

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        if self._fails_to_disconnect is not None:
            raise self._fails_to_disconnect

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
        if phone and not code and not password:
            # Ровно ветка настоящей библиотеки: второй запрос кода вместо входа.
            self.calls.append("send_code_request")
            return SentCode()
        if not code and not password:
            # И её же `else`: пустой пароль 2FA даёт ValueError, а не отказ.
            raise ValueError("You must provide a phone and a code the first time")
        self._authorized = not self._never_authorizes
        return object()

    async def is_user_authorized(self) -> bool:
        self.calls.append("is_user_authorized")
        return self._authorized

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        """Запрещённый метод отрабатывает — и остаётся в `calls`.

        Если бы его тут не было, `send_message` в `auth.py` уронил бы тест
        `AttributeError` изнутри чужого модуля, и из отчёта было бы не понять,
        что именно юзербот попытался отправить.
        """
        if name.startswith("_"):
            raise AttributeError(name)

        async def outgoing(*_args: object, **_kwargs: object) -> object:
            return None

        self.calls.append(name)
        return outgoing


class Recorder:
    """Консоль без терминала: помнит подсказки и отвечает заготовками."""

    def __init__(
        self,
        *,
        code: str | list[str] = "12345",
        password: str | list[str] = "секрет",  # noqa: S107 — заготовка, не пароль
        interrupt: bool = False,
        interactive: bool = True,
    ) -> None:
        self.said: list[str] = []
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self._codes = [code] if isinstance(code, str) else list(code)
        self._passwords = [password] if isinstance(password, str) else list(password)
        self._interrupt = interrupt
        self._interactive = interactive

    @property
    def console(self) -> Console:
        return Console(
            say=self.said.append,
            ask=self._ask,
            ask_secret=self._ask_secret,
            is_interactive=lambda: self._interactive,
        )

    @property
    def transcript(self) -> str:
        return "\n".join(self.said)

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._interrupt:
            raise KeyboardInterrupt
        # Последний ответ повторяется: так моделируется человек, который
        # упорно жмёт Enter, не сочиняя список из трёх пустых строк.
        return self._codes.pop(0) if len(self._codes) > 1 else self._codes[0]

    def _ask_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return self._passwords.pop(0) if len(self._passwords) > 1 else self._passwords[0]


def _ready_settings(*, tg_session: str = "") -> Settings:
    # _env_file=None: результат теста не должен зависеть от .env на машине.
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        tg_api_id=42,
        tg_api_hash="hash",
        tg_phone=PHONE,
        tg_session=tg_session,
    )


def _blank_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _run(
    tmp_path: Path,
    client: FakeClient | None,
    recorder: Recorder,
    *,
    settings: Settings | None = None,
    name: str = "tg_session.txt",
) -> tuple[int, Path]:
    out = tmp_path / name

    def factory(_api_id: int, _api_hash: str) -> FakeClient:
        if client is None:
            raise AssertionError("в Telegram ходить было незачем")
        return client

    code = run_auth(
        settings or _ready_settings(),
        recorder.console,
        out_path=out,
        client_factory=factory,
    )
    return code, out


def test_session_goes_to_a_file_and_never_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdout контейнера целиком уезжает в json-file на диск общей машины."""
    client = FakeClient()
    recorder = Recorder(code="54321")

    code, out = _run(tmp_path, client, recorder)

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION
    assert SESSION not in captured.out, "секрет в stdout заберёт докеровский логгер"
    assert SESSION not in captured.err
    assert SESSION not in recorder.transcript
    assert captured.out.strip() == str(out), "в stdout только путь, машиночитаемо"
    assert client.sign_in_args == [(PHONE, "54321", None)]
    assert set(client.calls) <= ALLOWED_CALLS, "юзербот только читает: лишних вызовов нет"
    assert client.calls[-1] == "disconnect", "соединение обязано закрыться"


@pytest.mark.skipif(sys.platform == "win32", reason="прав POSIX на Windows нет, прод — Linux")
def test_session_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    _, out = _run(tmp_path, FakeClient(), Recorder())

    assert out.stat().st_mode & 0o777 == 0o600


def test_existing_file_is_never_overwritten_and_telegram_is_not_touched(
    tmp_path: Path,
) -> None:
    """Проверка ДО входа: иначе авторизация создана, а сохранить её некуда."""
    out = tmp_path / "tg_session.txt"
    out.write_text("чужое содержимое", encoding="utf-8")
    recorder = Recorder()

    code, _ = _run(tmp_path, None, recorder)

    assert code == EXIT_NO_OUTPUT_FILE
    assert out.read_text(encoding="utf-8") == "чужое содержимое"
    assert "уже существует" in recorder.transcript


def test_broken_disconnect_does_not_lose_the_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Обрыв на закрытии — после кода и 2FA. Терять из-за него сессию нельзя."""
    client = FakeClient(fails_to_disconnect=ConnectionResetError("connection reset by peer"))
    recorder = Recorder()

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION
    assert "ConnectionResetError" in recorder.transcript, "об ошибке всё равно сказать надо"
    assert SESSION not in capsys.readouterr().out


def test_network_failure_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    client = FakeClient(fails_to_connect=OSError("Network is unreachable"))
    recorder = Recorder()

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_NETWORK, "код 1 из трейсбека в таблице кодов не описан"
    assert "OSError" in recorder.transcript
    assert not out.exists()


def test_timeout_is_reported_as_a_network_failure(tmp_path: Path) -> None:
    """`TimeoutError` — наследник OSError, и обязан идти тем же путём."""
    client = FakeClient(fails_to_connect=TimeoutError("timed out"))
    recorder = Recorder()

    code, _ = _run(tmp_path, client, recorder)

    assert code == EXIT_NETWORK
    assert "TimeoutError" in recorder.transcript


def test_interrupt_is_polite(tmp_path: Path) -> None:
    """`run_service` на Ctrl+C ведёт себя вежливо; интерактивная команда — тем более."""
    recorder = Recorder(interrupt=True)

    code, out = _run(tmp_path, FakeClient(), recorder)

    assert code == EXIT_INTERRUPTED
    assert "Прервано" in recorder.transcript
    assert not out.exists()


def test_code_prompt_says_the_code_arrives_in_telegram(tmp_path: Path) -> None:
    """Без этой подсказки владелец ждёт SMS, которой Telegram не пришлёт."""
    recorder = Recorder()

    _run(tmp_path, FakeClient(), recorder)

    assert recorder.prompts, "код обязаны спросить"
    assert "Telegram" in recorder.prompts[0]
    assert "SMS" in recorder.prompts[0]


def test_auth_without_settings_names_them_and_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()

    code, out = _run(tmp_path, None, recorder, settings=_blank_settings())

    assert code == EXIT_NOT_CONFIGURED, "тихий выход нулём выглядит как успех"
    for name in ("TG_API_ID", "TG_API_HASH", "TG_PHONE"):
        assert name in recorder.transcript
    assert not out.exists()
    assert capsys.readouterr().out == ""


def test_filled_session_gets_a_warning_about_the_second_one(tmp_path: Path) -> None:
    """Вторая сессия на аккаунте молча не создаётся: её потом отзывать руками."""
    recorder = Recorder()

    code, _ = _run(
        tmp_path,
        FakeClient(),
        recorder,
        settings=_ready_settings(tg_session="уже-есть"),
    )

    assert code == EXIT_OK
    assert "ВТОРУЮ" in recorder.transcript


def test_auth_asks_for_the_two_factor_password(tmp_path: Path) -> None:
    client = FakeClient(needs_password=True)
    recorder = Recorder(password="облачный-пароль")

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert recorder.secret_prompts, "аккаунт с 2FA обязан спросить пароль, а не упасть"
    assert client.sign_in_args[-1] == (None, None, "облачный-пароль")
    assert out.read_text(encoding="utf-8").strip() == SESSION


def test_telegram_refusal_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    """Владельцу нужно «подождите N секунд», а не стек вызовов."""
    client = FakeClient(refuses=FloodWaitError(request=None, capture=30))
    recorder = Recorder()

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_TELEGRAM_REFUSED
    assert "FloodWaitError" in recorder.transcript
    assert not out.exists(), "сессии нет — записывать нечего"
    assert client.calls[-1] == "disconnect", "соединение закрывается и на ошибке"


def test_session_never_reaches_python_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Границы проверки: только наш процесс — structlog, stdlib, stdout, stderr.

    Про докеровский `json-file` и про собственный логгер Telethon этот тест не
    знает ничего. Первое закрыто тем, что сессия не идёт в вывод вовсе (см.
    `test_session_goes_to_a_file_and_never_to_stdout`), второе — тем, что
    Telethon получает только `api_id`/`api_hash`, а строку собирает уже
    `StringSession.save()` в памяти.
    """
    recorder = Recorder()

    with caplog.at_level(logging.DEBUG), capture_logs() as events:
        code, out = _run(tmp_path, FakeClient(), recorder)

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION, "сессия обязана дойти до файла"
    assert SESSION not in caplog.text, "сессия утекла в stdlib-логи"
    assert all(SESSION not in repr(event) for event in events), "сессия утекла в structlog"
    assert SESSION not in captured.out
    assert SESSION not in captured.err
    assert SESSION not in recorder.transcript


async def test_outgoing_call_is_reported_by_name() -> None:
    """Страховка самого теста: запрещённый вызов виден и назван, а не AttributeError."""
    client = FakeClient()

    await client.send_message("кому", "что")

    assert "send_message" in client.calls
    assert set(client.calls) - ALLOWED_CALLS == {"send_message"}


def test_default_output_file_is_gitignored() -> None:
    """Файл с сессией не должен уехать в репозиторий с ближайшим `git add`."""
    from sniffer.collector.session_file import DEFAULT_SESSION_FILE

    root = Path(__file__).resolve().parent.parent
    assert DEFAULT_SESSION_FILE in (root / ".gitignore").read_text(encoding="utf-8").split()


def test_plain_start_is_untouched_by_the_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Аргумент `auth` — единственное отличие; без него процесс работает как раньше."""
    started: list[str] = []
    monkeypatch.setattr(collector_main, "run_service", lambda service: started.append(service.name))
    monkeypatch.setattr(collector_main, "run_auth", lambda out_path=None: 7)

    assert collector_main.main([]) == EXIT_OK
    assert started == ["collector"]

    assert collector_main.main(["auth"]) == 7
    assert started == ["collector"], "подкоманда не поднимает обычный процесс"


def test_auth_accepts_an_explicit_output_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`auth /secrets/tg_session.txt` — так документирован запуск в docker."""
    seen: list[str | None] = []

    def spy(out_path: str | None = None) -> int:
        seen.append(out_path)
        return EXIT_OK

    monkeypatch.setattr(collector_main, "run_auth", spy)

    assert collector_main.main(["auth", "/secrets/tg_session.txt"]) == EXIT_OK
    assert seen == ["/secrets/tg_session.txt"]


def test_output_path_is_not_read_from_the_environment() -> None:
    """Путь приходит аргументом: os.environ команда для этого не трогает."""
    assert "SNIFFER_SESSION_FILE" not in os.environ


def _ask_that_hits_end_of_input(_prompt: str) -> str:
    raise NoTerminalError("ввод закончился, кода нет")


def test_without_a_terminal_the_code_is_never_even_requested(tmp_path: Path) -> None:
    """`docker compose exec` без -it и `up` терминала не дают.

    Отправить код и не суметь его прочитать — худший вариант: следующая
    попытка уже ловит FloodWait. Поэтому проверка идёт до `connect`.
    """
    client = FakeClient()
    recorder = Recorder(interactive=False)

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_NO_TERMINAL
    assert client.calls == [], "в Telegram не ходили вовсе"
    assert not out.exists()
    assert "docker compose run" in recorder.transcript


def test_lost_terminal_mid_dialog_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    """Страховка: терминал может исчезнуть уже после проверки."""
    client = FakeClient()
    said: list[str] = []
    console = Console(
        say=said.append,
        ask=_ask_that_hits_end_of_input,
        ask_secret=lambda _p: "",
        is_interactive=lambda: True,
    )
    out = tmp_path / "tg_session.txt"

    code = run_auth(_ready_settings(), console, out_path=out, client_factory=lambda _i, _h: client)

    assert code == EXIT_NO_TERMINAL
    assert not out.exists()
    assert client.calls[-1] == "disconnect"


def test_client_introduces_itself_in_the_devices_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Безымянный клиент в «Устройствах» не отличить от чужого, когда его отзывают."""
    import telethon

    from sniffer import telegram_identity as identity
    from sniffer.collector import client as client_module

    seen: dict[str, object] = {}

    def fake_client(session: object, api_id: int, api_hash: str, **kwargs: object) -> object:
        seen.update(kwargs)
        seen["api_id"] = api_id
        return object()

    monkeypatch.setattr(telethon, "TelegramClient", fake_client)
    client_module.new_client(42, "hash")

    assert seen["api_id"] == 42
    assert seen["device_model"] == identity.DEVICE_MODEL
    assert seen["system_version"] == identity.SYSTEM_VERSION
    assert seen["app_version"] == identity.APP_VERSION
    assert "SnifferBot" in identity.DEVICE_MODEL


@pytest.mark.parametrize(
    "failure",
    [
        SecurityError("server sent invalid data"),
        InvalidChecksumError(1, 2),
        TypeNotFoundError(0xDEADBEEF, b""),
    ],
    ids=["security", "checksum", "unknown_tl_type"],
)
def test_protocol_error_is_a_documented_code_not_a_traceback(
    tmp_path: Path,
    failure: Exception,
) -> None:
    """Половина ошибок MTProto — ни `RPCError`, ни `OSError`, а прямо `Exception`.

    Рассинхрон msg_id, битый пакет, несовпадение TL-схемы: на машине с чужим
    прокси или инспекцией трафика это не экзотика. Без обработки владелец
    получает трейсбек и код 1, которого нет в таблице кодов `deploy.md`.
    """
    client = FakeClient(fails_to_connect=failure)
    recorder = Recorder()

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_PROTOCOL
    assert type(failure).__name__ in recorder.transcript
    assert not out.exists()


def test_protocol_handler_does_not_swallow_the_interrupt() -> None:
    """`except Exception` не должен проглотить Ctrl+C: тот от `BaseException`."""
    assert not issubclass(KeyboardInterrupt, Exception)
    assert not issubclass(asyncio.CancelledError, Exception)
    for name in ("SecurityError", "InvalidChecksumError", "TypeNotFoundError"):
        failure = getattr(telethon.errors, name)
        assert not issubclass(failure, RPCError), f"{name} мимо RPCError"
        assert not issubclass(failure, OSError), f"{name} мимо OSError"


def test_empty_code_never_reaches_sign_in(tmp_path: Path) -> None:
    """Enter вместо кода давал rc=0 и мусорную строку в .env.

    `TelegramClient.sign_in` понимает пустой код как «кода нет»: шлёт ВТОРОЙ
    запрос кода (шаг к FloodWait) и возвращает объект отправки. Исключения
    нет, а `session.save()` после `connect()` отдаёт правдоподобные 353
    символа неавторизованной сессии — владелец кладёт их в .env и узнаёт о
    поломке позже и в другом месте.
    """
    client = FakeClient()
    recorder = Recorder(code="")

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_NOT_SIGNED_IN
    assert not out.exists(), "нерабочая строка не должна доехать до файла"
    assert "sign_in" not in client.calls, "пустой код до библиотеки не доходит"
    assert client.calls.count("send_code_request") == 1, "второй запрос кода не отправлен"
    assert len(recorder.prompts) == 3, "переспросить бесплатно: сети тут нет"


def test_empty_code_once_is_only_a_reprompt(tmp_path: Path) -> None:
    """Случайный Enter не обязан стоить попытки: сети в переспрашивании нет."""
    client = FakeClient()
    recorder = Recorder(code=["", "54321"])

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION
    assert client.sign_in_args == [(PHONE, "54321", None)]


def test_empty_two_factor_password_never_reaches_sign_in(tmp_path: Path) -> None:
    """Пустой пароль давал ValueError → код 7 с советом искать прокси."""
    client = FakeClient(needs_password=True)
    recorder = Recorder(password="")

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_NOT_SIGNED_IN
    assert not out.exists()
    assert "прокси" not in recorder.transcript, "владельца нельзя гнать чинить сеть"
    assert len(recorder.secret_prompts) == 3


def test_session_is_saved_only_after_telegram_confirms_the_login(tmp_path: Path) -> None:
    """`session.save()` врёт: после `connect()` строка валидна и без входа."""
    client = FakeClient(never_authorizes=True)
    recorder = Recorder()

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_NOT_SIGNED_IN
    assert "is_user_authorized" in client.calls, "спрашиваем прямо, а не верим тишине"
    assert not out.exists()
    assert "нерабочая" in recorder.transcript


def test_broken_api_id_is_a_message_not_a_pydantic_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TG_API_ID=abc` — реалистичная опечатка: .env правят руками.

    Коммит f668a11 закрыл ПУСТОЕ значение; испорченное валило pydantic раньше
    любой нашей проверки, давая rc=1, которого нет в таблице кодов.
    """
    monkeypatch.setenv("TG_API_ID", "abc")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_PHONE", PHONE)
    get_settings.cache_clear()
    recorder = Recorder()

    def _must_not_connect(_api_id: int, _api_hash: str) -> FakeClient:
        raise AssertionError("со сломанными настройками в Telegram ходить незачем")

    try:
        code = run_auth(
            None,
            recorder.console,
            out_path=tmp_path / "tg_session.txt",
            client_factory=_must_not_connect,
        )
    finally:
        get_settings.cache_clear()

    assert code == EXIT_NOT_CONFIGURED
    assert "TG_API_ID" in recorder.transcript
    assert not (tmp_path / "tg_session.txt").exists()


def test_failed_write_after_login_leaves_nothing_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Диск кончился уже после входа: авторизация есть, файла быть не должно."""
    out = tmp_path / "tg_session.txt"

    def full_disk(path: Path, session: str) -> None:
        path.touch(mode=0o600)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(collector_auth, "write_session", full_disk)
    recorder = Recorder()

    code = run_auth(
        _ready_settings(),
        recorder.console,
        out_path=out,
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_NO_OUTPUT_FILE
    assert "No space left" in recorder.transcript
    assert "«Устройствах»" in recorder.transcript, "вторая сессия уже создана — сказать надо"


def test_partial_file_is_removed_so_the_retry_is_not_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Иначе советуемый повтор упрётся в собственный `O_EXCL` и даст код 5.

    Подменяется только поток записи — `O_EXCL`, права и уборка остаются
    настоящими.
    """
    out = tmp_path / "tg_session.txt"

    class FullDisk(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError(28, "No space left on device")

    def fake_fdopen(fd: int, *_args: object, **_kwargs: object) -> io.StringIO:
        os.close(fd)
        return FullDisk()

    monkeypatch.setattr(os, "fdopen", fake_fdopen)

    with pytest.raises(OSError, match="No space"):
        write_session(out, SESSION)

    monkeypatch.undo()
    assert not out.exists(), "огрызок заблокировал бы повтор"


def test_missing_directory_is_reported_before_telegram(tmp_path: Path) -> None:
    assert "нет" in why_cannot_write(tmp_path / "нет-такого" / "tg_session.txt")


def test_unwritable_directory_is_reported_before_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)

    assert "права записи" in why_cannot_write(tmp_path / "tg_session.txt")


def test_broken_symlink_is_reported_before_telegram(tmp_path: Path) -> None:
    """`exists()` идёт по симлинку, поэтому битый он считает пустым местом.

    Без отдельного `is_symlink()` отказ пришёл бы от `O_EXCL` уже после кода
    и двухфакторного пароля — когда авторизация на аккаунте создана.
    """
    link = tmp_path / "tg_session.txt"
    try:
        link.symlink_to(tmp_path / "которого-нет")
    except (OSError, NotImplementedError):
        pytest.skip("создание симлинков недоступно (Windows без прав)")

    assert not link.exists(), "битый симлинк — именно тот случай, что exists() пропускает"
    assert "симлинк" in why_cannot_write(link)


def test_closed_stdout_does_not_turn_success_into_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`… auth | head -1` закрывает stdout, а файл при этом записан."""

    def broken_pipe(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("builtins.print", broken_pipe)
    out = tmp_path / "tg_session.txt"

    code = run_auth(
        _ready_settings(),
        Recorder().console,
        out_path=out,
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION


def _console(
    *,
    say: Callable[[str], None] = lambda _t: None,
    ask: Callable[[str], str] = lambda _p: "12345",
    ask_secret: Callable[[str], str] = lambda _p: "секрет",
) -> Console:
    """Консоль с уже пройденной предпроверкой: тест целится в другое место."""
    return Console(say=say, ask=ask, ask_secret=ask_secret, is_interactive=lambda: True)


# --- Третье поколение rc=1: путь, на котором диагностика не доходила ---------
#
# Первые два поколения — необработанный `Exception` от MTProto (rc=7) и
# испорченный `TG_API_ID` (rc=2). Третье — исключение ИЗ САМОЙ ДИАГНОСТИКИ:
# проверка «куда писать» стоит вне `try`, а вывод сообщения об ошибке идёт
# внутри обработчика этой же ошибки. Оба способны бросить, и тогда наружу
# летит rc=1 с трейсбеком — код, которого в таблице `deploy.md` 3.5 не было.


class UnprobeablePath(Path):
    """Путь, который на вопрос о своём существовании отвечает EACCES.

    Так ведёт себя файл внутри каталога без права обхода — того самого, что
    получается из `install -d -m 700 secrets` без `-o 1000 -g 1000`. Подмена
    сделана подклассом, а не патчем `Path.exists`: `exists()` зовёт и сам
    pytest, когда печатает отчёт, и глобальный патч ломал бы прогон.
    """

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        raise PermissionError(13, "Permission denied")


def test_unprobeable_path_is_a_reason_not_a_traceback(tmp_path: Path) -> None:
    """`why_cannot_write` зовут ВНЕ `try` — бросить оттуда значит отдать rc=1.

    `Path.exists()` в 3.12 глотает только ENOENT, ENOTDIR, EBADF и ELOOP
    (`pathlib._ignore_error`); EACCES выходит наружу. Функция обязана
    вернуть строку при любом пути.
    """
    reason = why_cannot_write(UnprobeablePath(tmp_path, "tg_session.txt"))

    assert "Permission denied" in reason, "владельцу нужна причина, а не стек"


def test_unprobeable_path_becomes_a_documented_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вторые ворота: даже если проверка пути бросит, наружу идёт rc=5, не rc=1.

    Проверка «куда писать» — единственная, что стоит вне общего `try`, и
    именно на ней третий раз всплыл rc=1 с трейсбеком. Тест целится в место
    вызова, а не в саму функцию: он останется верным, даже если функцию
    когда-нибудь перепишут.
    """

    def denied(_path: Path) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(collector_auth, "why_cannot_write", denied)
    recorder = Recorder()

    # `client=None` в `_run` роняет тест, если фабрику всё-таки позвали:
    # отказ обязан прийти ДО отправки кода, иначе попытка сгорела впустую.
    code, out = _run(tmp_path, None, recorder)

    assert code == EXIT_NO_OUTPUT_FILE, "rc=1 из трейсбека в таблице кодов описан как наш баг"
    assert "Permission denied" in recorder.transcript
    assert not out.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="прав POSIX на Windows нет, прод — Linux")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root обходит проверки прав — воспроизводить нечего",
)
def test_directory_without_traverse_permission_is_reported_by_name(tmp_path: Path) -> None:
    """Настоящий каталог 0o000: `stat` на файл внутри отвечает EACCES.

    Ровно тот случай, что даёт `install -d -m 700` без смены владельца. Нужное
    сообщение («нет права записи или обхода») в модуле было и раньше — до него
    просто не доходило, потому что `exists()` стоял первым.
    """
    closed = tmp_path / "secrets"
    closed.mkdir()
    closed.chmod(0o000)
    try:
        reason = why_cannot_write(closed / "tg_session.txt")
    finally:
        closed.chmod(0o700)

    assert reason, "молчание тут означало бы попытку записи и трейсбек после входа"
    assert "права записи" in reason


@pytest.mark.skipif(sys.platform == "win32", reason="прав POSIX на Windows нет, прод — Linux")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root обходит проверки прав — воспроизводить нечего",
)
def test_symlink_into_a_closed_directory_is_reported_not_raised(tmp_path: Path) -> None:
    """`exists()` идёт по симлинку и падает EACCES на цели.

    Поэтому `is_symlink()` (он по `lstat`) спрашивается первым: ответ есть, и
    он верный, независимо от того, куда симлинк смотрит.
    """
    closed = tmp_path / "closed"
    closed.mkdir()
    link = tmp_path / "tg_session.txt"
    try:
        link.symlink_to(closed / "target")
    except (OSError, NotImplementedError):
        pytest.skip("создание симлинков недоступно")
    closed.chmod(0o000)
    try:
        reason = why_cannot_write(link)
    finally:
        closed.chmod(0o700)

    assert "симлинк" in reason


def test_a_path_too_long_to_probe_is_a_reason_not_a_traceback(tmp_path: Path) -> None:
    """ENAMETOOLONG — ещё один код, который `_ignore_error` не глотает."""
    reason = why_cannot_write(tmp_path / ("и" * 5000) / "tg_session.txt")

    assert reason, "пустая строка означала бы «можно писать»"


def test_dead_output_channel_does_not_eat_the_diagnostics(tmp_path: Path) -> None:
    """Переполненный том докеровского лога делал rc=1 из ЛЮБОЙ ошибки.

    `say` пишет в stderr, а на общей машине это тот же том, куда `json-file`
    складывает логи всех контейнеров. Кончилось место — `say` бросает
    `OSError(ENOSPC)`, попадает в `except OSError` («не достучаться до
    Telegram»), и `say` внутри самого обработчика бросает второй раз. Наружу
    улетал rc=1 и трейсбек — ровно там, где диагностика и была нужна.
    """
    said: list[str] = []

    def full_disk(text: str) -> None:
        said.append(text)
        raise OSError(28, "No space left on device")

    out = tmp_path / "tg_session.txt"

    code = run_auth(
        _ready_settings(),
        _console(say=full_disk),
        out_path=out,
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_OK, "файл записан — это успех, а не rc=1"
    assert out.read_text(encoding="utf-8").strip() == SESSION
    assert said, "сказать пытались — просто некуда"


def test_dead_output_channel_keeps_the_error_code_too(tmp_path: Path) -> None:
    """Тот же отказ вывода на ошибке: код обязан остаться своим, не 1."""

    def full_disk(_text: str) -> None:
        raise OSError(28, "No space left on device")

    client = FakeClient(refuses=FloodWaitError(request=None, capture=30))

    code = run_auth(
        _ready_settings(),
        _console(say=full_disk),
        out_path=tmp_path / "tg_session.txt",
        client_factory=lambda _i, _h: client,
    )

    assert code == EXIT_TELEGRAM_REFUSED, "отказ Telegram не превращается в rc=1"


def test_dead_output_channel_does_not_break_the_reprompt(tmp_path: Path) -> None:
    """Переспрос про пустой ввод тоже идёт через `say` — и тоже не вправе ронять."""
    codes = iter(["", "54321"])

    def full_disk(_text: str) -> None:
        raise OSError(28, "No space left on device")

    out = tmp_path / "tg_session.txt"

    code = run_auth(
        _ready_settings(),
        _console(say=full_disk, ask=lambda _p: next(codes)),
        out_path=out,
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_OK
    assert out.read_text(encoding="utf-8").strip() == SESSION


def test_a_broken_message_is_not_hidden_by_the_output_guard(tmp_path: Path) -> None:
    """Ворота глотают только `OSError`: наш баг в форматировании прятать не за что."""

    def our_bug(_text: str) -> None:
        raise KeyError("подставили не тот ключ")

    with pytest.raises(KeyError):
        run_auth(
            _ready_settings(),
            _console(say=our_bug),
            out_path=tmp_path / "tg_session.txt",
            client_factory=lambda _i, _h: FakeClient(),
        )


# --- Закрытые потоки и потеря терминала --------------------------------------


class FakeTty:
    """stdin, который считает себя терминалом."""

    closed = False

    def isatty(self) -> bool:
        return True

    def flush(self) -> None: ...


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_closed_output_stream_is_caught_before_the_code_is_spent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    """`auth 1>&- 2>&-` оставляет терминал на stdin, а `input()` уже не работает.

    У процесса с закрытым fd 1 `sys.stdout` равен `None`, и `input()` бросает
    `RuntimeError: lost sys.stdout` — не `OSError` и не `RPCError`, поэтому
    срабатывал финальный обработчик с текстом «сбой протокола MTProto,
    покажите разработчику». К этому моменту `connect` и `send_code_request`
    уже прошли: код отправлен и потрачен, а следующая попытка рискует
    FloodWait. Прежняя предпроверка этого не видела — она смотрела только
    `stdin.isatty()`.
    """
    monkeypatch.setattr(sys, stream, None)
    monkeypatch.setattr(sys, "stdin", FakeTty())
    out = tmp_path / "tg_session.txt"

    def must_not_connect(_api_id: int, _api_hash: str) -> FakeClient:
        raise AssertionError("код подтверждения тратить было нельзя")

    # is_interactive намеренно НЕ подменён: проверяется боевая предпроверка.
    code = run_auth(
        _ready_settings(),
        Console(say=lambda _t: None, ask=lambda _p: "12345", ask_secret=lambda _p: ""),
        out_path=out,
        client_factory=must_not_connect,
    )

    assert code == EXIT_NO_TERMINAL, "rc=7 «сбой MTProto» гнал владельца искать прокси"
    assert not out.exists()


def test_usable_console_requires_a_terminal_and_both_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", FakeTty())
    assert console_module._console_is_usable(), "терминал есть, потоки живы"

    monkeypatch.setattr(sys, "stderr", None)
    assert not console_module._console_is_usable()


def test_a_closed_fd_under_a_live_stream_is_not_a_usable_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Объект потока есть, а fd под ним закрыт — говорить всё равно негде.

    Ветка `except (OSError, ValueError)` в `_console_is_usable` до сих пор
    держалась комментарием: `closed` у такого объекта False, `isatty()` может
    ответить True, и только `flush()` выдаёт правду. Незакрытая ветка означала
    бы приглашение к вводу ПОСЛЕ отправки кода — то есть сгоревшую попытку.
    """

    class FlushRaises:
        closed = False

        def flush(self) -> None:
            raise ValueError("I/O operation on closed file")

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", FakeTty())
    monkeypatch.setattr(sys, "stdout", FlushRaises())

    assert not console_module._console_is_usable()

    # Второй способ той же беды: fd закрыт по-настоящему, и flush даёт OSError.
    class FlushFailsWithOsError(FlushRaises):
        def flush(self) -> None:
            raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(sys, "stdout", FlushFailsWithOsError())

    assert not console_module._console_is_usable()


def test_a_bad_descriptor_on_a_prompt_is_a_terminal_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OSError`/EBADF на приглашении — та же беда, что потерянный stdout.

    Комментарий в `_ask` перечисляет три способа (`EOFError`, `RuntimeError`,
    `OSError`), а тесты были на два: fd, закрытый из-под уже созданного объекта
    потока, оставался словами без проверки. Без неё `OSError` уехал бы в общий
    обработчик и стал бы rc=4 «сеть недоступна» — совет искать проблему в сети
    там, где нет терминала.
    """

    def bad_descriptor(_prompt: str = "") -> str:
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr("builtins.input", bad_descriptor)
    monkeypatch.setattr(sys, "stderr", None)

    with pytest.raises(NoTerminalError):
        console_module._ask("Код: ")

    monkeypatch.setattr(console_module, "getpass", bad_descriptor)

    with pytest.raises(NoTerminalError):
        console_module._ask_secret("Пароль: ")


@pytest.mark.skipif(os.name == "nt", reason="петля симлинков — POSIX; прод у нас Linux")
def test_a_symlink_loop_in_the_parent_is_a_named_refusal(tmp_path: Path) -> None:
    """Петля симлинков в каталоге-родителе — строка отказа, а не исключение.

    Ровно то, что `session_file` утверждал комментарием: ELOOP `_ignore_error`
    глотает, `is_dir()` отвечает False, и функция обязана вернуть текст. Тест
    нужен не ради самой петли, а ради контракта «не бросать»: бросок отсюда —
    это rc=1 с трейсбеком на последней проверке перед входом в Telegram.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.symlink_to(second)
    second.symlink_to(first)

    refusal = why_cannot_write(first / "tg_session.txt")

    assert refusal, "петля обязана назваться словами, а не улететь исключением"
    assert isinstance(refusal, str)


def test_human_output_never_falls_back_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`print(file=None)` уходит в stdout — а там машиночитаемый путь к файлу."""
    monkeypatch.setattr(sys, "stderr", None)

    console_module._say("подсказка человеку")

    assert capsys.readouterr().out == "", "текст для человека попал в машинный канал"


def test_lost_stdout_on_the_code_prompt_is_a_terminal_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`input()` без stdout бросает RuntimeError — ни OSError, ни RPCError."""

    def lost_stdout() -> str:
        raise RuntimeError("input(): lost sys.stdout")

    monkeypatch.setattr("builtins.input", lost_stdout)
    monkeypatch.setattr(sys, "stderr", None)

    with pytest.raises(NoTerminalError):
        console_module._ask("Код: ")


def test_end_of_input_on_the_two_factor_prompt_is_the_same_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Голый `getpass` бросал EOFError → rc=7, хотя на приглашении кода это rc=6.

    Одно и то же событие — конец ввода — не может давать разные коды в
    зависимости от того, на каком приглашении оно случилось. И совет
    «покажите разработчику, похоже на прокси» тут уводит в сторону.
    """

    def end_of_input(_prompt: str) -> str:
        raise EOFError

    # Подменяется сам `getpass`, а не обёртка: проверяется именно перевод
    # конца ввода в `NoTerminalError`, которого у голого getpass не было.
    monkeypatch.setattr(console_module, "getpass", end_of_input)
    said: list[str] = []
    client = FakeClient(needs_password=True)

    code = run_auth(
        _ready_settings(),
        _console(say=said.append, ask_secret=console_module._ask_secret),
        out_path=tmp_path / "tg_session.txt",
        client_factory=lambda _i, _h: client,
    )

    assert code == EXIT_NO_TERMINAL, "rc=7 советовал искать прокси там, где отвалился терминал"
    assert "прокси" not in "\n".join(said)


# --- Пробел в облачном пароле -------------------------------------------------


def test_two_factor_password_keeps_its_edge_spaces(tmp_path: Path) -> None:
    """`.strip()` на секрете — молчаливая порча того, что человек ввёл верно.

    Для цифрового кода обрезка правильна: краевой пробел из копипасты —
    мусор. Облачный пароль пробелом может и начинаться, и заканчиваться;
    обрезанный, он даёт rc=3 «пароль не тот», и владелец идёт искать ошибку в
    пароле, которого не портил.
    """
    client = FakeClient(needs_password=True)
    recorder = Recorder(password=" пробел по краям ")

    code, out = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert client.sign_in_args[-1] == (None, None, " пробел по краям ")
    assert out.read_text(encoding="utf-8").strip() == SESSION


def test_the_confirmation_code_still_loses_its_edge_spaces(tmp_path: Path) -> None:
    """Обратная сторона того же правила: код по-прежнему чистится."""
    client = FakeClient()
    recorder = Recorder(code="  54321  ")

    code, _ = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert client.sign_in_args == [(PHONE, "54321", None)]


def test_a_password_of_only_spaces_is_not_empty(tmp_path: Path) -> None:
    """Пробел — допустимый символ пароля; «пусто» про него говорить нельзя."""
    client = FakeClient(needs_password=True)
    recorder = Recorder(password="   ")

    code, _ = _run(tmp_path, client, recorder)

    assert code == EXIT_OK
    assert client.sign_in_args[-1] == (None, None, "   ")
    assert len(recorder.secret_prompts) == 1, "переспрашивать было незачем"


# --- Разбор аргументов --------------------------------------------------------


def test_an_unknown_subcommand_does_not_silently_start_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`notauth` уходил в сервис: аргумент проигнорирован, процесс живёт.

    Владелец при этом ждёт диалога авторизации, а видит обычный старт
    коллектора — и не понимает, почему его не спрашивают код.
    """
    started: list[str] = []
    monkeypatch.setattr(collector_main, "run_service", lambda service: started.append(service.name))

    code = collector_main.main(["notauth"])

    assert code == EXIT_USAGE
    assert started == [], "неизвестная подкоманда сервис не поднимает"


def test_extra_arguments_to_auth_are_an_error_not_a_silent_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth a b` записал бы сессию в `a`, выбросив `b` без единого слова."""

    def must_not_run(out_path: str | None = None) -> int:
        raise AssertionError("до auth доходить нельзя")

    monkeypatch.setattr(collector_main, "run_auth", must_not_run)

    assert collector_main.main(["auth", "a", "b"]) == EXIT_USAGE


def test_the_usage_hint_names_the_only_subcommand() -> None:
    assert "auth" in collector_main.USAGE


# --- Четвёртое поколение rc=1: полнота по построению, а не по списку типов ----
#
# Первые три поколения ловились перечислением: необработанный `Exception` от
# MTProto (rc=7), испорченный `TG_API_ID` (rc=2), `Path.exists()` вне `try`
# (rc=5). Каждый раз набор объявлялся полным — и каждый раз находился путь
# мимо списка. Четвёртый круг дал сразу два: `ValueError` от нулевого байта в
# пути (падал в `write_session`, то есть УЖЕ ПОСЛЕ созданной авторизации) и
# `PermissionError` на нечитаемом `.env` (падал в pydantic мимо
# `ValidationError`). Поэтому тесты ниже целятся не в эти два класса, а в саму
# привычку: они бросают тип, о котором код не знает, и требуют код из таблицы.

# Вся таблица `docs/deploy.md`, 3.5. rc=1 в ней описан как «наш баг» — ни один
# сценарий команды не вправе его вернуть.
DOCUMENTED_EXIT_CODES = frozenset(
    {
        EXIT_OK,
        EXIT_NOT_CONFIGURED,
        EXIT_TELEGRAM_REFUSED,
        EXIT_NETWORK,
        EXIT_NO_OUTPUT_FILE,
        EXIT_NO_TERMINAL,
        EXIT_PROTOCOL,
        EXIT_NOT_SIGNED_IN,
        EXIT_USAGE,
        EXIT_INTERRUPTED,
    }
)


class Boom(Exception):
    """Тип, о котором `auth.py` не знает и знать не может.

    Смысл именно в незнании: список `except (OSError, ValueError)` доказывает
    только то, что вспомнили при написании, а тест с чужим типом — что шаг
    охраняется по построению. Такой тест не устареет от новой версии Telethon
    или pydantic с новым классом исключения.
    """


def _boom(*_args: object, **_kwargs: object) -> object:
    raise Boom("что-то, чего никто не перечислял")


def test_a_null_byte_in_the_path_is_named_not_swallowed(tmp_path: Path) -> None:
    """Нулевой байт проходил ворота насквозь: `Path` отвечает «пусто», не «не знаю».

    `is_symlink`, `exists` и `is_dir` глотают `ValueError` из конвертера
    аргументов ровно так же, как ENOENT, поэтому `why_cannot_write` возвращала
    пустую строку («писать можно»), а отказ приходил из `os.open` в
    `write_session` — когда авторизация на аккаунте уже создана.
    """
    path = tmp_path / "tg\x00session.txt"

    assert path.parent.is_dir(), "родитель настоящий — врёт не он"
    assert path.is_symlink() is False, "вот оно: не «не знаю», а уверенное «нет»"
    assert path.exists() is False

    reason = why_cannot_write(path)

    assert "нулевой байт" in reason, "молчание тут означало бы «писать можно»"


def test_a_null_byte_in_the_parent_is_reported_too(tmp_path: Path) -> None:
    """Нулевой байт в имени каталога — тот же отказ, а не «каталога нет»."""
    assert "нулевой байт" in why_cannot_write(tmp_path / "се\x00креты" / "tg_session.txt")


def test_a_null_byte_in_the_path_never_reaches_telegram(tmp_path: Path) -> None:
    """Главное в этом баге — не код возврата, а МОМЕНТ отказа.

    Из оболочки такой путь не придёт (`execve` не пропускает нулевой байт в
    argv), но `run_auth(out_path=…)` — библиотечный вызов, и через него
    приходил. `client=None` в `_run` роняет тест, если фабрику всё-таки
    позвали: отказ обязан прийти ДО отправки кода, а не после входа.
    """
    recorder = Recorder()

    code, _ = _run(tmp_path, None, recorder, name="tg\x00session.txt")

    assert code == EXIT_NO_OUTPUT_FILE, "раньше здесь был ValueError и rc=1 с трейсбеком"
    assert code in DOCUMENTED_EXIT_CODES
    assert "нулевой байт" in recorder.transcript
    assert not list(tmp_path.iterdir()), "ни файла, ни огрызка"


def test_write_session_cleans_up_after_a_non_oserror_too(tmp_path: Path) -> None:
    """Огрызок убирается по любой причине отказа, а не только по `OSError`.

    Повтор команды упёрся бы в собственный `O_EXCL` («уже существует»), хотя
    вторая авторизация на аккаунте к тому моменту уже создана.
    """
    out = tmp_path / "tg_session.txt"

    class Unformattable:
        def __str__(self) -> str:
            raise Boom("сломались на форматировании самой строки")

    with pytest.raises(Boom):
        write_session(out, Unformattable())  # type: ignore[arg-type]

    assert not out.exists(), "огрызок заблокировал бы повтор"


def test_unreadable_env_is_a_documented_code_not_a_pydantic_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Нечитаемый `.env` — не экзотика, а нормальное состояние сервера.

    В `/var/www/sniffer` файл лежит `-rw------- root root`, а процесс в образе
    идёт под uid 1000: любой запуск с примонтированным репозиторием получал
    `PermissionError` изнутри pydantic — мимо `ValidationError` — и rc=1.
    """

    def unreadable_env() -> Settings:
        raise PermissionError(13, "Permission denied", ".env")

    monkeypatch.setattr(collector_auth, "get_settings", unreadable_env)
    recorder = Recorder()

    code = run_auth(
        None,
        recorder.console,
        out_path=tmp_path / "tg_session.txt",
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_NOT_CONFIGURED, "настройки не прочитаны — это код 2, как у пустых"
    assert "Permission denied" in recorder.transcript
    assert ".env" in recorder.transcript, "владельцу нужно знать, какой файл починить"
    assert not (tmp_path / "tg_session.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="прав POSIX на Windows нет, прод — Linux")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root читает файл 0o000 — воспроизводить нечего",
)
def test_pydantic_really_raises_permissionerror_on_an_unreadable_env(tmp_path: Path) -> None:
    """Тот же случай без подмен: настоящий файл 0o000 и настоящий pydantic.

    Тест удерживает не наш код, а факт о библиотеке, на котором построен
    предыдущий: `PermissionError` действительно проходит мимо
    `ValidationError`. Разойдись это с реальностью — узнать надо здесь.
    """
    env = tmp_path / ".env"
    env.write_text("TG_API_ID=42\n", encoding="utf-8")
    env.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            Settings(_env_file=str(env))  # type: ignore[call-arg]
    finally:
        env.chmod(0o600)


def _run_with_boom_at(
    step: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Подкладывает `Boom` в один шаг команды и возвращает её код выхода."""
    return _run_with_failure_at(step, Boom, tmp_path, monkeypatch)


def _run_with_failure_at(
    step: str,
    make_error: Callable[[], BaseException],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """То же, но исключение задаёт вызывающий — включая ветку `BaseException`.

    Существует потому, что ось типов у охраны ДВЕ, а закрыта была одна.
    `Boom(Exception)` проверял, что шаг не полагается на список ожидаемых
    классов; `KeyboardInterrupt` и `BaseExceptionGroup` проверяют, что сама
    граница взята по корню иерархии, а не по `Exception` — на пятом круге
    неполным оказался именно `Exception`.
    """

    def has_terminal() -> bool:
        return True

    def fail(*_args: object, **_kwargs: object) -> object:
        # Аргументы принимаются любые: заглушка встаёт и на `why_cannot_write(path)`,
        # и на `write_session(path, session)`. Без этого шаг падал бы `TypeError` —
        # тоже Exception, то есть тест проходил бы, не проверив прерывание.
        raise make_error()

    settings: Settings | None = _ready_settings()
    interactive: Callable[[], bool] = has_terminal

    if step == "settings":
        settings = None
        monkeypatch.setattr(collector_auth, "get_settings", fail)
    elif step == "terminal":
        interactive = fail  # type: ignore[assignment]
    elif step == "path":
        monkeypatch.setattr(collector_auth, "why_cannot_write", fail)
    elif step == "write":
        monkeypatch.setattr(collector_auth, "write_session", fail)
    elif step != "telegram":
        raise AssertionError(f"неизвестный шаг {step}")

    def factory(_api_id: int, _api_hash: str) -> FakeClient:
        if step == "telegram":
            raise make_error()
        return FakeClient()

    return run_auth(
        settings,
        _console_with(is_interactive=interactive),
        out_path=tmp_path / "tg_session.txt",
        client_factory=factory,
    )


def _console_with(*, is_interactive: Callable[[], bool]) -> Console:
    return Console(
        say=lambda _t: None,
        ask=lambda _p: "12345",
        ask_secret=lambda _p: "секрет",
        is_interactive=is_interactive,
    )


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ("settings", EXIT_NOT_CONFIGURED),
        ("terminal", EXIT_NO_TERMINAL),
        ("path", EXIT_NO_OUTPUT_FILE),
        ("telegram", EXIT_PROTOCOL),
        ("write", EXIT_NO_OUTPUT_FILE),
    ],
)
def test_every_step_answers_with_a_documented_code_for_an_unknown_failure(
    step: str,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вот чем закрывается набор кодов — и почему пятого круга быть не должно.

    Каждый шаг команды получает исключение ТИПА, КОТОРОГО КОД НЕ ЗНАЕТ, и
    обязан ответить кодом из таблицы `deploy.md`, 3.5. Пройти этот тест
    списком ожидаемых классов нельзя — только охраной по построению
    (`except Exception` последним в каждом шаге). Добавится шаг — добавится
    строка сюда; появится в библиотеке новый класс исключения — тест уже его
    покрывает.
    """
    code = _run_with_boom_at(step, tmp_path, monkeypatch)

    assert code == expected
    assert code in DOCUMENTED_EXIT_CODES
    assert code != 1, "rc=1 в таблице описан как наш баг: ни один сценарий сюда не ведёт"
    assert not (tmp_path / "tg_session.txt").exists(), "ни сессии, ни огрызка"


def test_a_cancelled_dialog_is_an_interrupt_not_a_traceback(tmp_path: Path) -> None:
    """`CancelledError` от `BaseException`, то есть мимо `except Exception`.

    Единственный путь в необработанную ошибку, который не закрывается охраной
    от `Exception`: `asyncio.run` отдаёт её наружу, когда задачу сняли. Исход
    ровно как у Ctrl+C — команду прервали.
    """
    recorder = Recorder()

    def cancelled(_api_id: int, _api_hash: str) -> FakeClient:
        raise asyncio.CancelledError

    code = run_auth(
        _ready_settings(),
        recorder.console,
        out_path=tmp_path / "tg_session.txt",
        client_factory=cancelled,
    )

    assert code == EXIT_INTERRUPTED
    assert code in DOCUMENTED_EXIT_CODES
    assert not (tmp_path / "tg_session.txt").exists()


def test_the_documented_codes_are_exactly_the_modules_constants() -> None:
    """Список выше — не отдельная память, а те же константы модуля.

    Появится новый код возврата, не попавший в `DOCUMENTED_EXIT_CODES` (а
    значит и в таблицу `deploy.md`) — падать надо здесь, а не у владельца.
    """
    constants = {
        value
        for name, value in vars(collector_auth).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }

    assert constants == set(DOCUMENTED_EXIT_CODES)
    assert 1 not in constants


# --------------------------------------------------------------------------
# Пятый круг: ось `BaseException`. Четыре предыдущих закрывали набор кодов
# списком, пятый показал, что и сама граница была не корнем иерархии:
# `except Exception` не берёт ни Ctrl+C, ни `BaseExceptionGroup`.
# --------------------------------------------------------------------------

GUARDED_STEPS: tuple[tuple[str, int], ...] = (
    ("settings", EXIT_NOT_CONFIGURED),
    ("terminal", EXIT_NO_TERMINAL),
    ("path", EXIT_NO_OUTPUT_FILE),
    ("telegram", EXIT_PROTOCOL),
    ("write", EXIT_NO_OUTPUT_FILE),
)


def _interrupt_group() -> BaseException:
    """Ctrl+C, завёрнутый в группу, — так его отдаёт `asyncio.TaskGroup`.

    Группа не наследует ни `KeyboardInterrupt`, ни `Exception`, поэтому её не
    берёт ни один `except` по конкретным классам: `BaseExceptionGroup` — это
    отдельная ветка иерархии, и раньше она давала литеральный rc=1.
    """
    return BaseExceptionGroup("группа", [KeyboardInterrupt()])


def _mixed_group() -> BaseException:
    """Снятая задача рядом с настоящим сбоем — это про сбой, не про прерывание."""
    return BaseExceptionGroup("группа", [asyncio.CancelledError(), Boom("настоящий сбой")])


def test_every_guarded_step_of_run_auth_is_in_the_matrix() -> None:
    """Список шагов связан с кодом механически, а не памятью автора теста.

    Прошлый круг оставил именно эту дыру: шагов в команде было шесть, в
    `parametrize` пять, и добавить шестой можно было, ничего не покрасив. Здесь
    сверяется само устройство — число блоков охраны в исходнике команды против
    числа шагов в матрице, — поэтому новый шаг красит тест до того, как его
    забудут проверить.

    И заодно проверяется сама граница: `except Exception` в команде быть не
    должно ни одного. `Exception` — не корень иерархии, и любое его появление
    здесь возвращает ту же дыру, из-за которой Ctrl+C давал трейсбек.
    """
    body = inspect.getsource(run_auth)

    assert body.count("except BaseException") == len(GUARDED_STEPS)
    assert "except Exception" not in body
    # Шаг «печать пути» живёт отдельной функцией и в матрицу не входит: у него
    # нет своего кода, исход уже определён записанным файлом. Охрана та же.
    assert inspect.getsource(collector_auth._report_success).count("except BaseException") == 1


@pytest.mark.parametrize(("step", "expected"), GUARDED_STEPS)
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
        pytest.param(asyncio.CancelledError, id="CancelledError"),
        pytest.param(_interrupt_group, id="BaseExceptionGroup"),
    ],
)
def test_an_interrupt_at_any_step_is_words_not_a_traceback(
    step: str,
    expected: int,
    make_error: Callable[[], BaseException],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C на ЛЮБОМ шаге — документированный код 130, а не стек.

    Раньше охрана от прерывания стояла на одном шаге из шести, дописанная
    вручную и по памяти, — то есть ровно тем способом, который в этом файле
    объявлен дефектом. Ctrl+C на остальных давал трейсбек, в том числе на шаге
    записи, то есть ПОСЛЕ созданной на аккаунте авторизации.

    `expected` здесь не используется: у прерывания исход один на все шаги, и
    это тоже утверждение — причина отказа сменилась, а не место.
    """
    code = _run_with_failure_at(step, make_error, tmp_path, monkeypatch)

    assert code == EXIT_INTERRUPTED
    assert code in DOCUMENTED_EXIT_CODES
    assert code != 1, "rc=1 в таблице описан как наш баг"
    assert not (tmp_path / "tg_session.txt").exists()


@pytest.mark.parametrize(("step", "expected"), GUARDED_STEPS)
def test_a_mixed_group_answers_about_the_failure_not_the_interrupt(
    step: str,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Группа со снятой задачей И настоящим сбоем — ответ про сбой.

    Иначе «прервано» стало бы удобной свалкой: любая группа с одной снятой
    задачей внутри скрыла бы настоящую причину и код шага.
    """
    code = _run_with_failure_at(step, _mixed_group, tmp_path, monkeypatch)

    assert code == expected
    assert code in DOCUMENTED_EXIT_CODES


@pytest.mark.parametrize(("step", "expected"), GUARDED_STEPS)
def test_systemexit_is_passed_through_not_rewritten(
    step: str,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SystemExit` проходит наружу: чужой код выхода не переписываем своим.

    Единственный карв-аут в охране — и он проверяется, потому что «поймано» и
    «переброшено» здесь одинаково выглядят в коде и по-разному в поведении.
    """
    with pytest.raises(SystemExit):
        _run_with_failure_at(step, lambda: SystemExit(3), tmp_path, monkeypatch)


def test_an_interrupt_while_reporting_success_keeps_the_success(tmp_path: Path) -> None:
    """Ctrl+C на печати пути не отменяет записанный файл.

    Файл уже на диске с правами 0600, авторизация создана: превратить это в
    ошибку из-за прерывания на необязательном последнем действии нельзя.
    """
    path = tmp_path / "tg_session.txt"

    def interrupt(_text: str) -> None:
        raise KeyboardInterrupt

    monkeypatched = Console(
        say=lambda _t: None,
        ask=lambda _p: "12345",
        ask_secret=lambda _p: "секрет",
        is_interactive=lambda: True,
    )
    code = run_auth(
        _ready_settings(),
        monkeypatched,
        out_path=path,
        client_factory=lambda _i, _h: FakeClient(),
    )

    assert code == EXIT_OK
    assert path.exists()

    # Тот же случай, но прерывание приходит из самого `print`.
    second = tmp_path / "second.txt"
    with mock.patch("builtins.print", side_effect=interrupt):
        code = run_auth(
            _ready_settings(),
            monkeypatched,
            out_path=second,
            client_factory=lambda _i, _h: FakeClient(),
        )

    assert code == EXIT_OK
    assert second.exists(), "файл записан — значит успех, чем бы ни кончилась печать"


def test_a_broken_str_of_an_exception_still_names_its_class(tmp_path: Path) -> None:
    """Класс исключения в сообщении есть, даже если его текст падает сам.

    Иначе охрана всё равно кончилась бы трейсбеком — уже своим, из строки
    диагностики, — и владелец опять получил бы rc=1 вместо кода из таблицы.
    """

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("текст исключения сам сломан")

    recorder = Recorder()

    def unprintable(_api_id: int, _api_hash: str) -> FakeClient:
        raise Unprintable

    code = run_auth(
        _ready_settings(),
        recorder.console,
        out_path=tmp_path / "tg_session.txt",
        client_factory=unprintable,
    )

    assert code == EXIT_PROTOCOL
    assert any("Unprintable" in line for line in recorder.said)


def test_a_cancelled_disconnect_does_not_lose_the_created_session(tmp_path: Path) -> None:
    """Ctrl+C на закрытии соединения не отменяет уже полученную сессию.

    `_close_quietly` живёт в `finally`, а исключение оттуда заменяет собой
    возвращаемое значение. Для `OSError` это было закрыто, для прерывания — нет,
    хотя цена та же и хуже: авторизация на аккаунте создана, строка потеряна.
    """
    path = tmp_path / "tg_session.txt"

    class InterruptOnDisconnect(FakeClient):
        async def disconnect(self) -> None:
            raise KeyboardInterrupt

    code = run_auth(
        _ready_settings(),
        _console_with(is_interactive=lambda: True),
        out_path=path,
        client_factory=lambda _i, _h: InterruptOnDisconnect(),
    )

    assert code == EXIT_OK
    assert path.exists(), "сессия получена — терять её из-за прерывания на disconnect нельзя"
