"""Коллектор: единственный держатель MTProto-сессии.

Коллектор разбирает очередь безопасного вступления: не более одного кандидата
за проход, с лимитами в Postgres. Заодно он дочитывает последние 200 сообщений
каждого из первых десяти активных чатов после `chats.last_msg_id`, складывает
их в `raw_messages` и находит перекрёстные ссылки. Live-подписка придёт позже.

Юзербот не общается (CLAUDE.md): нет ни сообщений, ни реакций, ни отметок о
прочтении. Единственные разрешённые исключения здесь — вступление по очереди и
беззвучный режим; их лимиты и журнал хранятся в Postgres.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import structlog

from sniffer.collector.auth import EXIT_OK, EXIT_USAGE, run_auth
from sniffer.collector.console import Console, tell
from sniffer.collector.discovery import DiscoveryRunner
from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service

log = structlog.get_logger(__name__)

NAME = "collector"

# Разовая ручная операция живёт подкомандой того же модуля, а не отдельным
# скриптом: авторизация нужна ровно тому образу, в котором потом крутится
# коллектор, и версия Telethon у них обязана быть одна.
AUTH_COMMAND = "auth"

# Лимит разрешает одно вступление не чаще чем раз в час; проверять его каждые
# 15 минут достаточно, а держать второе MTProto-подключение постоянно нельзя:
# в момент живого поиска сессией пользуется адаптер Telegram-групп.
POLL_INTERVAL_S = 15 * 60.0


def missing_settings(settings: Settings) -> list[str]:
    """Без строки сессии авторизоваться в контейнере всё равно негде.

    Интерактивный ввод кода подтверждения в фоновом процессе невозможен,
    поэтому `TG_SESSION` такое же обязательное требование, как ключи
    приложения. Строку выдаёт подкоманда `auth` — разовая ручная операция.
    """
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_SESSION": bool(settings.tg_session.strip()),
    }
    return [name for name, filled in required.items() if not filled]


async def run(stop: asyncio.Event) -> None:
    log.info("collector.started")
    runner = DiscoveryRunner()
    await idle_loop(stop, runner.tick, service=NAME, poll_interval_s=POLL_INTERVAL_S)


SERVICE = Service(name=NAME, requires=missing_settings, run=run)


USAGE = (
    "Использование: python -m sniffer.collector [auth [файл]]\n"
    "  без аргументов — обычный процесс коллектора;\n"
    f"  {AUTH_COMMAND} [файл] — разовая интерактивная авторизация юзербота."
)


def main(argv: Sequence[str]) -> int:
    """Без аргументов — обычный процесс; `auth [файл]` — разовая авторизация.

    Путь приходит аргументом, а не переменной окружения: в контейнере он
    указывает на смонтированный каталог, и это решение места запуска, а не
    конфигурация сервиса.

    Разбор строгий: **любой аргумент, кроме `auth`, — это ошибка, а не повод
    поднять сервис**. `python -m sniffer.collector notauth` (опечатка, старое
    имя подкоманды, лишний флаг) молча уходил в демона: аргумент
    проигнорирован, процесс живёт, владелец ждёт диалога авторизации, которого
    не будет, и видит в логе обычный старт коллектора. То же с лишним
    позиционным аргументом у `auth`: `auth a b` записал бы сессию в `a`, а `b`
    выбросил, хотя человек, скорее всего, ошибся именно в первом.
    """
    if not argv:
        run_service(SERVICE)
        return EXIT_OK

    if argv[0] != AUTH_COMMAND:
        tell(Console(), f"Неизвестная подкоманда: {argv[0]!r}.\n{USAGE}")
        return EXIT_USAGE

    if len(argv) > 2:
        tell(Console(), f"Лишние аргументы: {list(argv[2:])}.\n{USAGE}")
        return EXIT_USAGE

    return run_auth(out_path=argv[1] if len(argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
