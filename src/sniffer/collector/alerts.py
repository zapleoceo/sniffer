"""Оповещения владельцу о состоянии юзербота через обычный Bot API."""

from __future__ import annotations

import structlog

from sniffer.config import Settings

log = structlog.get_logger(__name__)


async def session_unavailable(settings: Settings, error: str) -> None:
    """Сказать владельцу, что сессию нужно переавторизовать, без секрета.

    Оповещение отправляет публичный бот, а не юзербот. Текст содержит только
    класс ошибки: StringSession, код подтверждения и любые данные Telethon не
    попадают ни в Telegram, ни в логи.
    """
    if not settings.bot_token.strip() or not settings.owner_chat_id:
        log.warning("collector.session_alert_not_configured")
        return
    from aiogram import Bot

    bot = Bot(settings.bot_token)
    try:
        await bot.send_message(
            settings.owner_chat_id,
            "RecVN: сессия чтения Telegram недоступна "
            f"({error}). Поиск по группам остановлен; переавторизуйте её в панели.",
        )
    except Exception as exc:
        log.warning("collector.session_alert_failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        await bot.session.close()
