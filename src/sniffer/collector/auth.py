"""Разовая интерактивная авторизация юзербота: выдаёт `StringSession`.

Нужна ровно один раз на аккаунт. Telethon умеет хранить сессию файлом своего
формата, но такой файл не переживает пересоздание контейнера, поэтому владелец
получает строку и кладёт её в `.env` как `TG_SESSION`.

**Строка сессии равносильна паролю от аккаунта, поэтому в stdout её нет:**
весь вывод контейнера забирает докеровский `json-file`. Сессия уходит в файл с
правами 0600, наружу — только путь к нему; где именно и почему — `session_file`
(там же сказано, что файл промежуточный, а целевое место — БД).

Юзербот только читает (CLAUDE.md, spec-v2 6.1). Список доступных методов
Telegram и то, как аккаунт подписан в «Устройствах», — в `client`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import ValidationError
from telethon.errors import RPCError, SessionPasswordNeededError

from sniffer.collector.client import ClientFactory, TelegramLike, new_client
from sniffer.collector.console import (
    Console,
    EmptyInputError,
    NoTerminalError,
    ask_required,
)
from sniffer.collector.session_file import (
    DEFAULT_SESSION_FILE,
    why_cannot_write,
    write_session,
)
from sniffer.config import Settings, get_settings

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_TELEGRAM_REFUSED = 3
EXIT_NETWORK = 4
EXIT_NO_OUTPUT_FILE = 5
EXIT_NO_TERMINAL = 6
EXIT_PROTOCOL = 7
EXIT_NOT_SIGNED_IN = 8
# 128 + SIGINT — то, что оболочка ожидает увидеть после Ctrl+C.
EXIT_INTERRUPTED = 130

# Код подтверждения Telegram присылает СООБЩЕНИЕМ В САМ TELEGRAM, а не в SMS.
# Без этой подсказки владелец полминуты смотрит в телефон и ждёт эсэмэску,
# которой не будет.
CODE_PROMPT = "Код подтверждения (придёт сообщением в Telegram, не в SMS): "
PASSWORD_PROMPT = "Пароль двухфакторной защиты (ввод не отображается): "


def _broken_settings(err: ValidationError) -> str:
    """Имена сломанных переменных так, как они названы в `.env`."""
    names = {str(part).upper() for issue in err.errors() for part in issue["loc"]}
    return ", ".join(sorted(names)) or "не разобрать, какая переменная"


class NotSignedInError(RuntimeError):
    """Диалог закончился, а авторизации на аккаунте так и нет."""


def missing_auth_settings(settings: Settings) -> list[str]:
    """Имена пустых настроек так, как они названы в `.env`.

    `TG_SESSION` здесь не нужен — его эта команда и создаёт.
    """
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_PHONE": bool(settings.tg_phone.strip()),
    }
    return [name for name, filled in required.items() if not filled]


async def _close_quietly(client: TelegramLike, console: Console) -> None:
    """Закрытие соединения не вправе испортить уже полученную сессию.

    Исключение из `finally` заменяет собой возвращаемое значение: оборвалась
    сеть на `disconnect` — и владелец, уже прошедший код и двухфакторный
    пароль, получает трейсбек вместо строки, а на аккаунте висит созданная
    авторизация. Поэтому здесь широкий `except`: ошибку показываем, но ронять
    ею результат нельзя.
    """
    try:
        await client.disconnect()
    except Exception as err:
        console.say(f"Соединение закрылось с ошибкой ({type(err).__name__}); на сессию не влияет.")


async def authorize(
    settings: Settings,
    console: Console,
    *,
    client_factory: ClientFactory = new_client,
) -> str:
    """Проводит вход и возвращает строку сессии. Не печатает и не логирует её."""
    phone = settings.tg_phone.strip()
    client = client_factory(settings.tg_api_id, settings.tg_api_hash.strip())
    await client.connect()
    try:
        console.say(f"Запрашиваю код для {phone}…")
        await client.send_code_request(phone)
        try:
            await client.sign_in(phone, ask_required(console, CODE_PROMPT))
        except SessionPasswordNeededError:
            # Аккаунт с включённой двухфакторной защитой: код принят, но
            # Telegram ждёт ещё и облачный пароль.
            await client.sign_in(password=ask_required(console, PASSWORD_PROMPT, secret=True))
        # Спрашиваем прямо, а не верим отсутствию исключения: `connect()` уже
        # обменялся ключами, поэтому `session.save()` вернёт правдоподобную
        # строку и у НЕавторизованной сессии. Такая строка выглядит рабочей,
        # молча ложится в .env и ломается позже и в другом месте.
        if not await client.is_user_authorized():
            raise NotSignedInError("Telegram не подтвердил вход")
        session = client.session.save()
    finally:
        await _close_quietly(client, console)
    return session


def run_auth(
    settings: Settings | None = None,
    console: Console | None = None,
    *,
    out_path: str | os.PathLike[str] | None = None,
    client_factory: ClientFactory = new_client,
) -> int:
    """Команда целиком: проверки, вход, запись файла. Возвращает код выхода."""
    console = console or Console()
    path = Path(out_path or DEFAULT_SESSION_FILE)

    if settings is None:
        try:
            settings = get_settings()
        except ValidationError as err:
            # `TG_API_ID=abc` — реалистичная опечатка: .env правят руками.
            # Пустое значение закрыто в config.py, испорченное — нет, и
            # pydantic роняет процесс раньше любой нашей проверки.
            console.say(f"Настройки не читаются: {_broken_settings(err)}. Поправьте .env.")
            return EXIT_NOT_CONFIGURED

    missing = missing_auth_settings(settings)
    if missing:
        console.say(
            f"Не хватает настроек: {', '.join(missing)}. "
            "Заполните их в .env и повторите — авторизоваться без них негде."
        )
        return EXIT_NOT_CONFIGURED

    if settings.tg_session.strip():
        console.say(
            "Внимание: TG_SESSION уже заполнен. Новая авторизация создаст ВТОРУЮ "
            "сессию на аккаунте — старую придётся отозвать вручную "
            "(Telegram → Настройки → Устройства)."
        )

    if not console.is_interactive():
        # Проверяем ДО отправки кода: Telegram уже прислал бы его, а прочитать
        # было бы некому — и на следующую попытку прилетит FloodWait. Терминал
        # даёт `docker compose run`; `exec` без `-it` и `up` — нет.
        console.say(
            "Нужен интерактивный терминал: код подтверждения вводит человек. "
            "На сервере запускайте через `docker compose run` (не `exec` и не `up`)."
        )
        return EXIT_NO_TERMINAL

    blocked = why_cannot_write(path)
    if blocked:
        console.say(f"Записывать сессию некуда: {blocked}.")
        return EXIT_NO_OUTPUT_FILE

    try:
        session = asyncio.run(authorize(settings, console, client_factory=client_factory))
    except RPCError as err:
        # Класс и текст ошибки Telegram, но не трейсбек: владельцу нужно
        # «код неверный» или «подождите N секунд», а не стек.
        console.say(f"Telegram отказал: {type(err).__name__}: {err}")
        return EXIT_TELEGRAM_REFUSED
    except OSError as err:
        # Сеть и таймауты: TimeoutError с версии 3.3 — наследник OSError.
        console.say(f"Не достучаться до Telegram: {type(err).__name__}: {err}")
        return EXIT_NETWORK
    except NoTerminalError as err:
        console.say(f"Некому ввести код: {err}. Запускайте через `docker compose run`.")
        return EXIT_NO_TERMINAL
    except (EmptyInputError, NotSignedInError) as err:
        console.say(
            f"Вход не состоялся: {err}. Сессия не сохранена — повторите команду. "
            "Строку из неудачной попытки в .env класть нельзя: она нерабочая."
        )
        return EXIT_NOT_SIGNED_IN
    except KeyboardInterrupt:
        # Ctrl+C на интерактивной команде — обычный способ передумать.
        console.say("Прервано, сессия не создана.")
        return EXIT_INTERRUPTED
    except Exception as err:
        # Половина ошибок MTProto не наследует ни RPCError, ни OSError:
        # SecurityError, BadMessageError, InvalidChecksumError,
        # TypeNotFoundError, AuthKeyNotFound, MultiError, ReadCancelledError —
        # все прямо от Exception. Это рассинхрон msg_id, битый пакет,
        # несовпадение TL-схемы; на машине с чужим прокси или инспекцией
        # трафика они реальны. Без этой ветки владелец получает трейсбек и
        # код 1, которого нет в таблице кодов. KeyboardInterrupt и
        # CancelledError сюда не попадают: они от BaseException.
        console.say(
            f"Неожиданная ошибка: {type(err).__name__}: {err}. Сессия не сохранена. "
            "Это либо сбой протокола MTProto (битый пакет, прокси или инспекция "
            "трафика по пути), либо наш баг — покажите эту строку разработчику."
        )
        return EXIT_PROTOCOL

    try:
        write_session(path, session)
    except OSError as err:
        # Сессию не печатаем даже здесь: докер-логгер заберёт её так же, как
        # из любой другой строки stdout. Повтор команды создаст новую.
        console.say(
            f"Сессия получена, но записать её в {path} не вышло: {err}. "
            "Огрызок файла убран, так что команду можно повторять сразу; "
            "неудачную авторизацию отзовите в «Устройствах»."
        )
        return EXIT_NO_OUTPUT_FILE

    _report_success(console, path)
    return EXIT_OK


def _report_success(console: Console, path: Path) -> None:
    """Сессия уже на диске, и рассказать о ней — не повод потерять успех.

    `python -m sniffer.collector auth | head -1` закрывает stdout, и `print`
    отвечает `BrokenPipeError`. Файл при этом записан, права выставлены: это
    успех, а не сбой, и трейсбек тут дезинформирует.
    """
    try:
        console.say(
            f"Строка сессии записана в {path} (права 0600). Впишите её в .env как "
            "TG_SESSION и удалите файл: shred -u или rm."
        )
        print(path)
    except OSError:
        pass
