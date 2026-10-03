"""Ответы Bot API настоящими типами aiogram, без сети.

Сессия подставная, а разбор ответа — настоящий: исключение из HTTP-статуса и тела
делает `BaseSession.check_response` самой aiogram, не тест. Поэтому «403 стал
`TelegramForbiddenError`» проверяет библиотеку, а не наше представление о ней, и
тест не устареет молча, когда она сменит класс или текст ошибки.

Сессия не принимает произвольные аргументы: сигнатуры `make_request`, `close` и
`stream_content` — те же, что у абстрактного класса. Заглушка с `**kwargs` приняла бы
и неверный вызов, и тест остался бы зелёным (CLAUDE.md, правило 5).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, NamedTuple

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage, TelegramMethod

TOKEN = "123456:AAsniffer-test-bot-token"


class Reply(NamedTuple):
    """Ответ Bot API: HTTP-статус и тело как есть."""

    status: int
    body: str


def refusal(status: int, description: str, **parameters: int) -> Reply:
    """Отказ Telegram: `ok=false`, код и описание, при 429 — `retry_after`."""
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return Reply(status, json.dumps(body))


SENT = Reply(
    200,
    json.dumps(
        {
            "ok": True,
            "result": {
                "message_id": 5,
                "date": 1_760_000_000,
                "chat": {"id": 42, "type": "private"},
                "text": "x",
            },
        }
    ),
)


class ScriptedSession(BaseSession):
    """Сессия по сценарию: ответ, исключение (сеть) или, когда сценарий кончился, «отправлено»."""

    def __init__(self, *script: Reply | BaseException) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._script = list(script)

    async def make_request(  # сигнатура — как у абстрактного класса, включая `timeout`
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.calls.append(method)
        step = self._script.pop(0) if self._script else SENT
        if isinstance(step, BaseException):
            raise step
        return self.check_response(bot, method, step.status, step.body).result

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""


def scripted_bot(*script: Reply | BaseException) -> tuple[Bot, ScriptedSession]:
    session = ScriptedSession(*script)
    return Bot(TOKEN, session=session), session


def telegram_error(reply: Reply) -> TelegramAPIError:
    """Настоящее исключение aiogram на этот ответ: его строит `check_response`, не мы."""
    session = ScriptedSession()
    bot = Bot(TOKEN, session=session)
    try:
        session.check_response(bot, SendMessage(chat_id=42, text="x"), reply.status, reply.body)
    except TelegramAPIError as error:
        return error
    raise AssertionError(f"ответ {reply.status} не стал исключением: сверять нечего")
