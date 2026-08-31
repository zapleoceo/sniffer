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


def _reraise_if_not_ours(err: BaseException) -> None:
    """`SystemExit` и `GeneratorExit` — не поломка, а требование остановиться.

    Единственный карв-аут в охране, и он именно ПЕРЕбрасывает, а не глотает:
    просьбу выйти с чужим кодом нельзя переписать своим, а проглоченный
    `GeneratorExit` ломает контракт интерпретатора. Оба живут одним местом,
    чтобы шаг не выбирал сам: шагу остаётся только корень иерархии.
    """
    if isinstance(err, SystemExit | GeneratorExit):
        raise err


def _is_interrupt(err: BaseException) -> bool:
    """Прервали ли команду: Ctrl+C, снятая задача — или ГРУППА из них.

    Группа проверяется отдельно, потому что `BaseExceptionGroup` не наследует
    ни `KeyboardInterrupt`, ни `Exception`: `asyncio.TaskGroup` и
    `asyncio.timeout` заворачивают снятую задачу именно в неё, и ни один
    `except` по конкретным классам её не берёт. В установленном Telethon
    `TaskGroup` сегодня нет — но зависимость обновляется без нашего участия, а
    цена промаха здесь ровно тот rc=1 с трейсбеком, который закрывается уже
    пятый круг.

    Смешанная группа прерыванием НЕ считается: если рядом со снятой задачей
    приехал настоящий сбой, ответ обязан быть про сбой.
    """
    if isinstance(err, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(err, BaseExceptionGroup):
        return all(_is_interrupt(sub) for sub in err.exceptions)
    return False


def _describe(err: BaseException) -> str:
    """«Класс: текст» — и класс останется, даже если текст бросает сам."""
    name = type(err).__name__
    try:
        return f"{name}: {err}"
    except BaseException:
        return name


def _failed(
    console: Console,
    err: BaseException,
    code: int,
    problem: str,
    advice: str = "",
    interrupted_advice: str = "",
) -> int:
    """Ответ шага на ЛЮБУЮ поломку: слова владельцу и документированный код.

    Принимает `BaseException`, а не `Exception`, и это разница между «закрыто
    построением» и «закрыто списком». `Exception` — не корень иерархии:
    `KeyboardInterrupt`, `CancelledError` и `BaseExceptionGroup` проходят мимо
    него, и набор кодов объявлялся полным четыре круга подряд именно потому,
    что эту границу принимали за корень. Ниже `BaseException` типов нет —
    добавить в зависимость новый класс исключений так, чтобы он обошёл охрану,
    больше нельзя.
    """
    _reraise_if_not_ours(err)
    if _is_interrupt(err):
        tell(console, f"Прервано, сессия не создана.{interrupted_advice}")
        return EXIT_INTERRUPTED
    tell(console, f"{problem} ({_describe(err)}).{advice}")
    return code


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
    ею результат нельзя. Широкий он до КОРНЯ иерархии, а не до `Exception`:
    Ctrl+C во время закрытия соединения стоит ровно того же — сессия получена,
    авторизация на аккаунте создана, и потерять строку из-за прерывания на
    самом последнем, уже необязательном действии нельзя. `SystemExit` и
    `GeneratorExit` перебрасываются (`_reraise_if_not_ours`): просьбу
    остановиться подменять своим исходом нельзя даже здесь.
    """
    try:
        await client.disconnect()
    except BaseException as err:
        _reraise_if_not_ours(err)
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
    `except` — `BaseException`, то есть КОРЕНЬ иерархии, и отвечает он через
    `_failed()`. Список ожидаемых классов оказывался неполным четыре раза
    (сеть, протокол MTProto, `PermissionError` на каталоге без обхода,
    `ValueError` от нулевого байта, `PermissionError` на нечитаемом `.env`), а
    на пятый раз неполным оказался сам `Exception`: Ctrl+C давал трейсбек на
    четырёх шагах из шести, включая шаг ПОСЛЕ созданной авторизации, а
    `BaseExceptionGroup` — литеральный rc=1 на всех шести. Поэтому граница
    здесь одна и она последняя в иерархии: ниже неё новых типов не появится.

    Вне охраны намеренно оставлен только вывод диагностики: падение
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
    except BaseException as err:
        # Файл `.env` может быть и нечитаемым: на сервере он лежит
        # `-rw------- root root`, а процесс в образе идёт под uid 1000 — и
        # pydantic отвечает `PermissionError` изнутри чтения файла, мимо
        # `ValidationError`. Тот же ответ подходит любой другой причине «не
        # смогли прочитать настройки»: назвать переменную нечем, но код тот
        # же, что у пустой или испорченной настройки. Ctrl+C здесь тоже обязан
        # быть словами, а не стеком, — потому охрана до корня иерархии.
        return _failed(
            console,
            err,
            EXIT_NOT_CONFIGURED,
            "Настройки не читаются",
            " Проверьте, что .env на месте и доступен этому пользователю на чтение.",
        )

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
    except BaseException as err:
        # Не смогли даже выяснить, есть ли с кем говорить, — значит говорить
        # не с кем. Исход тот же, что у явного «терминала нет»: код 6.
        return _failed(console, err, EXIT_NO_TERMINAL, "Не проверить терминал")

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
    except BaseException as err:
        # Вторые ворота к тому же ответу. `why_cannot_write` обязана вернуть
        # строку при любом пути и сама ловит всё, что умеет бросить — но это
        # последняя проверка перед входом в Telegram, и её будущая правка не
        # должна превращаться в rc=1 с трейсбеком. Здесь же построение самого
        # `Path`: делать его вне охраны значит оставить точку броска в шаге,
        # который целиком про «куда писать». Ctrl+C на этом шаге особенно
        # вероятен: прямо перед ним печатается предупреждение про ВТОРУЮ
        # сессию, и передумать в ответ на него — нормальная реакция.
        return _failed(
            console, err, EXIT_NO_OUTPUT_FILE, "Записывать сессию некуда: путь не проверить"
        )
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
    except BaseException as err:
        # Половина ошибок MTProto не наследует ни RPCError, ни OSError:
        # SecurityError, BadMessageError, InvalidChecksumError,
        # TypeNotFoundError, AuthKeyNotFound, MultiError, ReadCancelledError —
        # все прямо от Exception. Это рассинхрон msg_id, битый пакет,
        # несовпадение TL-схемы; на машине с чужим прокси или инспекцией
        # трафика они реальны. Ctrl+C и снятая задача приходят сюда же:
        # отдельной ветки для них больше нет, их отличает `_failed`, и потому
        # забыть их на следующем шаге нельзя — как забыли на четырёх прошлых.
        return _failed(
            console,
            err,
            EXIT_PROTOCOL,
            "Неожиданная ошибка",
            " Сессия не сохранена. Это либо сбой протокола MTProto (битый пакет, "
            "прокси или инспекция трафика по пути), либо наш баг — покажите эту "
            "строку разработчику.",
        )

    try:
        write_session(path, session)
    except BaseException as err:
        # Сессию не печатаем даже здесь: докер-логгер заберёт её так же, как
        # из любой другой строки stdout. Повтор команды создаст новую.
        # Тип не перечисляем: это самое дорогое место команды — авторизация на
        # аккаунте уже создана, и rc=1 с трейсбеком вместо совета «отзовите в
        # Устройствах» стоит владельцу висящей сессии. `ValueError` от
        # нулевого байта в пути приходил ровно сюда.
        #
        # У прерывания здесь свой совет, и это не косметика: Ctrl+C ПОСЛЕ
        # созданной авторизации оставляет на аккаунте живую сессию, о которой
        # владелец иначе не узнает. «Сессия не создана» тут было бы ложью —
        # создана, не записана только строка.
        return _failed(
            console,
            err,
            EXIT_NO_OUTPUT_FILE,
            f"Сессия получена, но записать её в {path} не вышло",
            " Огрызок файла убран, так что команду можно повторять сразу; "
            "неудачную авторизацию отзовите в «Устройствах».",
            " Авторизация на аккаунте УЖЕ создана, а строка не записана: отзовите "
            "сессию в «Устройствах», иначе она останется висеть. Повторять команду "
            "можно сразу.",
        )

    _report_success(console, path)
    return EXIT_OK


def _report_success(console: Console, path: Path) -> None:
    """Сессия уже на диске, и рассказать о ней — не повод потерять успех.

    `python -m sniffer.collector auth | head -1` закрывает stdout, и `print`
    отвечает `BrokenPipeError`. Файл при этом записан, права выставлены: это
    успех, а не сбой, и трейсбек тут дезинформирует. Тот же `OSError(ENOSPC)`
    прилетает, когда переполнился том докеровского лога.

    Поэтому `print` охраняется по построению — от `BaseException`, то есть от
    корня иерархии, а не от списка и даже не от `Exception`: после записи файла
    исход команды уже определён, и ни причина отказа вывода, ни Ctrl+C в этот
    момент не вправе превратить записанный файл в ошибку. А вот `tell` ниже намеренно не охраняется:
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
    except BaseException as err:
        # Причина отказа вывода исхода не меняет — файл уже на диске, — поэтому
        # тип не перечисляем: и `BrokenPipeError` от `| head -1`, и ENOSPC на
        # переполненном томе, и Ctrl+C означают здесь одно и то же. Просьбу
        # процессу остановиться переписывать всё равно нельзя — она одна.
        _reraise_if_not_ours(err)
        return
