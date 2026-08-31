"""Переавторизация юзербота через форму: номер → код → при 2FA пароль.

Единственное место во всём проекте, где секрет проходит через HTTP, и правила
здесь жёстче обычных:

- **облачный пароль и код не логируются и не сохраняются** — они живут только в
  POST-запросе, ровно чтобы довершить `sign_in`;
- **строка сессии не попадает ни в HTML, ни в лог, ни в историю браузера** —
  из потока она уходит прямо в шифрование и в базу;
- **состояние потока живёт в памяти процесса с TTL**, а не в cookie: незаконченный
  вход не должен оставлять на диске ничего.

Telethon импортируется внутри функций намеренно: без переавторизации дашборд
обязан подниматься, даже если у Telethon проблемы с окружением.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from sniffer.config import get_settings
from sniffer.dashboard import data

log = structlog.get_logger(__name__)

# Десять минут на ввод кода. Больше — это открытый незавершённый вход, который
# никто не закроет.
FLOW_TTL_S = 600
# Текст исключения Telethon показываем обрезанным: в первых символах класс
# ошибки и номер, дальше начинается внутренняя кухня библиотеки.
MAX_ERROR_CHARS = 200


class ReauthError(Exception):
    """Понятная владельцу причина, почему вход не идёт."""


@dataclass(slots=True)
class Flow:
    client: Any
    phone: str
    code_hash: str
    needs_password: bool = False
    touched_at: float = field(default_factory=time.monotonic)


_flows: dict[str, Flow] = {}


async def prune() -> None:
    """Забытые потоки закрываем: каждый держит открытое соединение с Telegram."""
    now = time.monotonic()
    for flow_id, flow in list(_flows.items()):
        if now - flow.touched_at > FLOW_TTL_S:
            _flows.pop(flow_id, None)
            with contextlib.suppress(Exception):
                await flow.client.disconnect()


def missing_settings() -> list[str]:
    settings = get_settings()
    return [
        name
        for name, value in (
            ("TG_API_ID", settings.tg_api_id),
            ("TG_API_HASH", settings.tg_api_hash),
        )
        if not value
    ]


async def start(phone: str) -> str:
    """Выслать код и открыть поток. Возвращает id потока для формы."""
    await prune()
    missing = missing_settings()
    if missing:
        raise ReauthError(f"не заведено: {', '.join(missing)}")

    number = phone.strip() or get_settings().tg_phone.strip()
    if not number:
        raise ReauthError("укажите номер телефона")

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings = get_settings()
    client = TelegramClient(
        StringSession(),
        settings.tg_api_id,
        settings.tg_api_hash,
        device_model="SnifferBot collector",
        system_version="docker",
        app_version="0.1",
    )
    await client.connect()
    try:
        sent = await client.send_code_request(number)
    except Exception as exc:
        await client.disconnect()
        # Номер в лог, текст ошибки в лог; ни кода, ни пароля здесь ещё нет.
        log.warning("reauth.send_code_failed", phone=number, error=str(exc)[:MAX_ERROR_CHARS])
        raise ReauthError(f"Telegram не выслал код: {str(exc)[:MAX_ERROR_CHARS]}") from exc

    flow_id = secrets.token_urlsafe(16)
    _flows[flow_id] = Flow(client=client, phone=number, code_hash=sent.phone_code_hash)
    return flow_id


async def verify(flow_id: str, *, code: str = "", password: str = "") -> str:
    """Довершить вход. Возвращает номер, для которого сохранена сессия.

    Ни `code`, ни `password` не попадают ни в лог, ни в ответ: они нужны ровно
    для одного вызова `sign_in` и после него забываются.
    """
    await prune()
    flow = _flows.get(flow_id)
    if flow is None:
        raise ReauthError("поток истёк — начните заново")

    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    flow.touched_at = time.monotonic()
    try:
        if flow.needs_password:
            if not password.strip():
                raise ReauthError("введите облачный пароль")
            await flow.client.sign_in(password=password.strip())
        else:
            if not code.strip():
                raise ReauthError("введите код")
            await flow.client.sign_in(flow.phone, code.strip(), phone_code_hash=flow.code_hash)
    except SessionPasswordNeededError:
        flow.needs_password = True
        raise NeedsPassword from None
    except ReauthError:
        raise
    except Exception as exc:
        log.warning("reauth.sign_in_failed", phone=flow.phone, error=str(exc)[:MAX_ERROR_CHARS])
        raise ReauthError(f"Telegram отказал: {str(exc)[:MAX_ERROR_CHARS]}") from exc

    # Строка сессии живёт в локальной переменной до шифрования и наружу не
    # возвращается ни при каком исходе.
    session_string = StringSession.save(flow.client.session)
    await flow.client.disconnect()
    _flows.pop(flow_id, None)
    await data.save_session(flow.phone, session_string)
    log.info("reauth.session_saved", phone=flow.phone)
    return flow.phone


class NeedsPassword(Exception):
    """У аккаунта включён облачный пароль — нужен второй шаг."""


def needs_password(flow_id: str) -> bool:
    flow = _flows.get(flow_id)
    return flow is not None and flow.needs_password
