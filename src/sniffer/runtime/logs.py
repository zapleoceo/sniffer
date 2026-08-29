"""Настройка структурного лога, общая для всех процессов.

Формат выбирается по тому, куда пишем: в терминале — человекочитаемый рендер,
в контейнере — JSON, потому что `docker logs` и любой сборщик разбирают строку
машиной, а не глазами.

Модуль называется `logs`, а не `logging`: одноимённый с стандартной
библиотекой модуль внутри пакета читается как ошибка, даже когда абсолютные
импорты делают его безопасным.
"""

from __future__ import annotations

import logging
import sys

import structlog

DEFAULT_LEVEL = "INFO"

# Чужие библиотеки на INFO шумят так, что свои события в логе тонут: aiogram
# печатает каждый апдейт, httpx — каждый запрос. Поднимаем им порог отдельно.
NOISY_LOGGERS = ("aiogram.event", "httpx", "httpcore", "asyncio", "telethon")


def setup_logging(level: str = DEFAULT_LEVEL) -> None:
    """Идемпотентна: повторный вызов переопределяет настройку, а не дублирует её."""
    numeric = logging.getLevelNamesMapping().get(level.strip().upper(), logging.INFO)

    # force=True, потому что зависимости успевают добавить свой handler на
    # импорте, и без него basicConfig молча ничего бы не сделала.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric, force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _renderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        # Через stdlib, чтобы события структлога и чужие записи шли одним
        # потоком в одном порядке.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _renderer() -> structlog.typing.Processor:
    if sys.stdout.isatty():
        return structlog.dev.ConsoleRenderer()
    # ensure_ascii=False обязателен: сообщения и доменные строки здесь
    # по-русски, и в escape-последовательностях лог нечитаем.
    return structlog.processors.JSONRenderer(ensure_ascii=False)
