"""Что значит отказ Bot API для строки очереди: пять исходов вместо одного «ошибка».

Раньше любая ошибка была одинаковой: «подожди пятнадцать минут, попробуй ещё
дважды». Но Telegram говорит о разном, и разное требует разного. 403 — клиент
заблокировал бота, и каждая следующая попытка бесполезна. 429 — подождать столько,
сколько просят, и не тратить на это попытку сообщения. 400 — сообщение некорректно,
и повтор даст то же самое. Сеть и 5xx — подождать с нарастающей паузой.

Класс исключения здесь не решает, что случилось, а только подсказывает: решение
принимает `classify`, и всё, чего он не узнал, считается временным сбоем с потолком
попыток. Список узнаваемого — таблица данных, а не ветвление: новый случай — новая
строка, а не новая ветка.

Охрана идёт до корня иерархии (CLAUDE.md, «Как закрывают набор кодов возврата»):
шаг отправки ловит `BaseException`, а решение, что из этого не исход, а просьба
остановиться, принимает одна функция — `must_propagate`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

DETAIL_LIMIT = 200
# Токен бота (`123456:AAH...`) попадает в текст сетевой ошибки вместе с адресом
# запроса, а текст уходит в лог и в `outbox.last_error`. Секрету там не место.
_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


class Kind(StrEnum):
    BLOCKED = "blocked"  # 403 и «чат недоступен»: писать этому клиенту нельзя
    RATE_LIMITED = "rate_limited"  # 429: Telegram просит подождать
    REJECTED = "rejected"  # 400: отказ именно этому сообщению, повтор даст то же
    SYSTEM = "system"  # 401/404: сломан токен — виноват не клиент и не сообщение
    TRANSIENT = "transient"  # сеть, 5xx, всё неузнанное: повторим позже


@dataclass(frozen=True, slots=True)
class Failure:
    kind: Kind
    reason: str  # короткий код для `outbox.last_error`: forbidden, too_long, ...
    detail: str = ""  # текст ошибки без токена, не длиннее DETAIL_LIMIT
    retry_after: int = 0  # секунды; осмысленно только у RATE_LIMITED


# Что Telegram пишет в описании 400. Порядок значим: первое совпадение побеждает.
# Строка таблицы: (код причины, исход, подстроки описания в нижнем регистре).
BAD_REQUESTS: tuple[tuple[str, Kind, tuple[str, ...]], ...] = (
    (
        "chat_unavailable",
        Kind.BLOCKED,
        ("chat not found", "peer_id_invalid", "user not found", "user is deactivated"),
    ),
    ("too_long", Kind.REJECTED, ("message is too long",)),
    ("thread_not_found", Kind.REJECTED, ("message thread not found", "topic_deleted")),
    ("bad_markup", Kind.REJECTED, ("can't parse entities", "can't find end of")),
)


def scrub(text: str) -> str:
    """Текст ошибки без токена бота и не длиннее лимита колонки."""
    return _TOKEN.sub("<token>", text)[:DETAIL_LIMIT]


def is_interrupt(exc: BaseException) -> bool:
    """Просьба остановиться: Ctrl+C, отмена задачи или группа, где есть хоть одно из них.

    Смешанная группа тоже считается прерыванием: проглотить в ней
    `KeyboardInterrupt` значило бы процесс, который не останавливается по Ctrl+C.
    """
    if isinstance(exc, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(is_interrupt(inner) for inner in exc.exceptions)
    return False


def must_propagate(exc: BaseException) -> bool:
    """Исходы, которые не наши: чужой код выхода и просьба остановиться.

    Единственное место, где это решается: шаг отправки не выбирает сам, он только
    ловит корень. `SystemExit` своим кодом не переписывается, проглоченный
    `GeneratorExit` ломает контракт интерпретатора.
    """
    return isinstance(exc, SystemExit | GeneratorExit) or is_interrupt(exc)


def classify(exc: BaseException) -> Failure:
    """Исключение шага отправки → исход. Прерывания не классифицирует, а возвращает наружу."""
    if must_propagate(exc):
        raise exc
    if isinstance(exc, TelegramRetryAfter):
        return Failure(Kind.RATE_LIMITED, "rate_limited", _detail(exc), max(1, exc.retry_after))
    if isinstance(exc, TelegramForbiddenError):
        return Failure(Kind.BLOCKED, "forbidden", _detail(exc))
    if isinstance(exc, TelegramUnauthorizedError | TelegramNotFound):
        return Failure(Kind.SYSTEM, "token_rejected", _detail(exc))
    if isinstance(exc, TelegramBadRequest):
        return _bad_request(exc)
    if isinstance(exc, TelegramMigrateToChat):
        return Failure(Kind.REJECTED, "migrated", _detail(exc))
    return Failure(Kind.TRANSIENT, "transient", _detail(exc))


def _bad_request(exc: TelegramBadRequest) -> Failure:
    text = exc.message.lower()
    for reason, kind, markers in BAD_REQUESTS:
        if any(marker in text for marker in markers):
            return Failure(kind, reason, _detail(exc))
    return Failure(Kind.REJECTED, "bad_request", _detail(exc))


def _detail(exc: BaseException) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return scrub(f"{type(exc).__name__}: {message}")
