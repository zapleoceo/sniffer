"""Подкоманда `python -m sniffer.collector auth`.

Главная проверка здесь — не «работает ли вход», а **куда девается строка
сессии**. Строка равносильна паролю от аккаунта, поэтому её единственный
выход — файл с правами 0600, а в stdout уходит только путь к нему: весь вывод
контейнера забирает докеровский `json-file` и кладёт на диск общей машины.

Вторая по важности проверка — что уже полученную сессию нельзя потерять:
ни на обрыве соединения при закрытии, ни на трейсбеке.

Живой сети тут нет: Telethon-клиент подменён фейком, который записывает,
какие методы дёрнули. Заодно это показывает, что юзербот ничего не отправляет.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

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

    from sniffer.collector import client as client_module

    seen: dict[str, object] = {}

    def fake_client(session: object, api_id: int, api_hash: str, **kwargs: object) -> object:
        seen.update(kwargs)
        seen["api_id"] = api_id
        return object()

    monkeypatch.setattr(telethon, "TelegramClient", fake_client)
    client_module.new_client(42, "hash")

    assert seen["api_id"] == 42
    assert seen["device_model"] == client_module.DEVICE_MODEL
    assert seen["system_version"] == client_module.SYSTEM_VERSION
    assert seen["app_version"] == client_module.APP_VERSION
    assert "SnifferBot" in client_module.DEVICE_MODEL


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
