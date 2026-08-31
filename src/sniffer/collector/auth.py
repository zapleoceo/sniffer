"""Разовая интерактивная авторизация юзербота: выдаёт `StringSession`.

Нужна ровно один раз на аккаунт. Telethon умеет хранить сессию файлом своего
формата, но такой файл не переживает пересоздание контейнера, поэтому владелец
получает строку и кладёт её в `.env` как `TG_SESSION`.

**Строка сессии равносильна паролю от аккаунта, поэтому в stdout её нет:**
весь вывод контейнера забирает докеровский `json-file`. Сессия уходит в файл с
правами 0600, наружу — только путь к нему; где именно и почему — `session_file`
(там же сказано, что файл промежуточный, а целевое место — БД).

Юзербот только читает (CLAUDE.md, spec-v2 6.1). Список доступных методов
Telegram — в `client`, подпись сеанса в «Устройствах» — в
`sniffer.telegram_identity` (её обязан повторить и боевой читатель сессии).

**Каждая строка для человека уходит через `console.tell`, а не через
`console.say` напрямую.** Вывод — не безотказный канал: на общей машине
докеровский `json-file` пишет в тот же том, и переполнение тома делает stderr
источником `OSError(ENOSPC)`. Прямой `say` внутри обработчика ошибки бросал бы
второе исключение из первого, и наружу летел бы rc=1 с трейсбеком вместо
диагностики. Все коды возврата этого модуля описаны в `docs/deploy.md`, 3.5 —
код, которого нет в той таблице, считается дефектом.

**Полнота этой таблицы держится построением, а не памятью:** каждый шаг команды
стоит внутри блока, чей последний `except` — `Exception`, а не список ожидаемых
классов. Список уже четыре раза оказывался неполным; правило и способ его
проверять — в CLAUDE.md, «Как закрывают набор кодов возврата».
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
    tell,
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
EXIT_USAGE = 9
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
        tell(
            console, f"Соединение закрылось с ошибкой ({type(err).__name__}); на сессию не влияет."
        )


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
        tell(console, f"Запрашиваю код для {phone}…")
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
    """Команда целиком: проверки, вход, запись файла. Возвращает код выхода.

    Каждый шаг, который делает РАБОТУ, стоит внутри блока, чей последний
    `except` — `Exception`, а не список ожидаемых классов. Список уже четыре
    раза оказывался неполным (сеть, протокол MTProto, `PermissionError` на
    каталоге без обхода, `ValueError` от нулевого байта и `PermissionError` на
    нечитаемом `.env`), поэтому полнота здесь держится построением, а не
    памятью. Вне охраны намеренно оставлен только вывод диагностики: падение
    на форматировании нашей же строки — наш баг, и прятать его не за что
    (`tell` глотает лишь `OSError`, то есть «канал не работает»).
    """
    console = console or Console()

    try:
        if settings is None:
            settings = get_settings()
        missing = missing_auth_settings(settings)
        session_already_set = bool(settings.tg_session.strip())
    except ValidationError as err:
        # `TG_API_ID=abc` — реалистичная опечатка: .env правят руками.
        # Пустое значение закрыто в config.py, испорченное — нет, и
        # pydantic роняет процесс раньше любой нашей проверки.
        tell(console, f"Настройки не читаются: {_broken_settings(err)}. Поправьте .env.")
        return EXIT_NOT_CONFIGURED
    except Exception as err:
        # Файл `.env` может быть и нечитаемым: на сервере он лежит
        # `-rw------- root root`, а процесс в образе идёт под uid 1000 — и
        # pydantic отвечает `PermissionError` изнутри чтения файла, мимо
        # `ValidationError`. Тот же ответ подходит любой другой причине «не
        # смогли прочитать настройки»: назвать переменную нечем, но код тот
        # же, что у пустой или испорченной настройки.
        tell(
            console,
            f"Настройки не читаются ({type(err).__name__}: {err}). "
            "Проверьте, что .env на месте и доступен этому пользователю на чтение.",
        )
        return EXIT_NOT_CONFIGURED

    if missing:
        tell(
            console,
            f"Не хватает настроек: {', '.join(missing)}. "
            "Заполните их в .env и повторите — авторизоваться без них негде.",
        )
        return EXIT_NOT_CONFIGURED

    if session_already_set:
        tell(
            console,
            "Внимание: TG_SESSION уже заполнен. Новая авторизация создаст ВТОРУЮ "
            "сессию на аккаунте — старую придётся отозвать вручную "
            "(Telegram → Настройки → Устройства).",
        )

    try:
        interactive = console.is_interactive()
    except Exception as err:
        # Не смогли даже выяснить, есть ли с кем говорить, — значит говорить
        # не с кем. Исход тот же, что у явного «терминала нет»: код 6.
        tell(console, f"Не проверить терминал ({type(err).__name__}: {err}).")
        return EXIT_NO_TERMINAL

    if not interactive:
        # Проверяем ДО отправки кода: Telegram уже прислал бы его, а прочитать
        # было бы некому — и на следующую попытку прилетит FloodWait. Терминал
        # даёт `docker compose run`; `exec` без `-it` и `up` — нет. Закрытые
        # stdout/stderr проверяются тем же вопросом (см. `_console_is_usable`):
        # с ними `input()` падает уже ПОСЛЕ отправки кода, то есть попытка
        # сгорает. Сказать об этом там же и некуда — весь ответ в коде выхода.
        tell(
            console,
            "Нужен интерактивный терминал и живые stdout/stderr: код подтверждения "
            "вводит человек, а приглашение к вводу надо где-то напечатать. "
            "На сервере запускайте через `docker compose run` (не `exec` и не `up`).",
        )
        return EXIT_NO_TERMINAL

    try:
        path = Path(out_path or DEFAULT_SESSION_FILE)
        blocked = why_cannot_write(path)
    except Exception as err:
        # Вторые ворота к тому же ответу. `why_cannot_write` обязана вернуть
        # строку при любом пути и сама ловит всё, что умеет бросить — но это
        # последняя проверка перед входом в Telegram, и её будущая правка не
        # должна превращаться в rc=1 с трейсбеком. Здесь же построение самого
        # `Path`: делать его вне охраны значит оставить точку броска в шаге,
        # который целиком про «куда писать».
        tell(console, f"Записывать сессию некуда: путь не проверить ({type(err).__name__}: {err}).")
        return EXIT_NO_OUTPUT_FILE
    if blocked:
        tell(console, f"Записывать сессию некуда: {blocked}.")
        return EXIT_NO_OUTPUT_FILE

    try:
        session = asyncio.run(authorize(settings, console, client_factory=client_factory))
    except RPCError as err:
        # Класс и текст ошибки Telegram, но не трейсбек: владельцу нужно
        # «код неверный» или «подождите N секунд», а не стек.
        tell(console, f"Telegram отказал: {type(err).__name__}: {err}")
        return EXIT_TELEGRAM_REFUSED
    except OSError as err:
        # Сеть и таймауты: TimeoutError с версии 3.3 — наследник OSError.
        tell(console, f"Не достучаться до Telegram: {type(err).__name__}: {err}")
        return EXIT_NETWORK
    except NoTerminalError as err:
        tell(console, f"Некому ввести код: {err}. Запускайте через `docker compose run`.")
        return EXIT_NO_TERMINAL
    except (EmptyInputError, NotSignedInError) as err:
        tell(
            console,
            f"Вход не состоялся: {err}. Сессия не сохранена — повторите команду. "
            "Строку из неудачной попытки в .env класть нельзя: она нерабочая.",
        )
        return EXIT_NOT_SIGNED_IN
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C на интерактивной команде — обычный способ передумать.
        # `CancelledError` здесь рядом не для красоты: она от `BaseException`,
        # то есть мимо `except Exception` ниже, и `asyncio.run` отдаёт её
        # наружу, когда цикл сняли задачу. Исход тот же — команду прервали.
        tell(console, "Прервано, сессия не создана.")
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
        tell(
            console,
            f"Неожиданная ошибка: {type(err).__name__}: {err}. Сессия не сохранена. "
            "Это либо сбой протокола MTProto (битый пакет, прокси или инспекция "
            "трафика по пути), либо наш баг — покажите эту строку разработчику.",
        )
        return EXIT_PROTOCOL

    try:
        write_session(path, session)
    except Exception as err:
        # Сессию не печатаем даже здесь: докер-логгер заберёт её так же, как
        # из любой другой строки stdout. Повтор команды создаст новую.
        # Тип не перечисляем: это самое дорогое место команды — авторизация на
        # аккаунте уже создана, и rc=1 с трейсбеком вместо совета «отзовите в
        # Устройствах» стоит владельцу висящей сессии. `ValueError` от
        # нулевого байта в пути приходил ровно сюда.
        tell(
            console,
            f"Сессия получена, но записать её в {path} не вышло: {err}. "
            "Огрызок файла убран, так что команду можно повторять сразу; "
            "неудачную авторизацию отзовите в «Устройствах».",
        )
        return EXIT_NO_OUTPUT_FILE

    _report_success(console, path)
    return EXIT_OK


def _report_success(console: Console, path: Path) -> None:
    """Сессия уже на диске, и рассказать о ней — не повод потерять успех.

    `python -m sniffer.collector auth | head -1` закрывает stdout, и `print`
    отвечает `BrokenPipeError`. Файл при этом записан, права выставлены: это
    успех, а не сбой, и трейсбек тут дезинформирует. Тот же `OSError(ENOSPC)`
    прилетает, когда переполнился том докеровского лога.

    Поэтому `print` охраняется по построению — от `Exception`, не от списка:
    после записи файла исход команды уже определён, и никакая причина отказа
    вывода не вправе его изменить. А вот `tell` ниже намеренно не охраняется:
    он глотает `OSError` («канал не работает») и пропускает наш баг в
    форматировании строки — прятать его не за что.
    """
    tell(
        console,
        f"Строка сессии записана в {path} (права 0600). Впишите её в .env как "
        "TG_SESSION и удалите файл: shred -u или rm.",
    )
    try:
        print(path)
    except Exception:
        # Причина отказа вывода исхода не меняет — файл уже на диске, — поэтому
        # тип не перечисляем: и `BrokenPipeError` от `| head -1`, и ENOSPC на
        # переполненном томе, и что угодно ещё означают здесь одно и то же.
        return
