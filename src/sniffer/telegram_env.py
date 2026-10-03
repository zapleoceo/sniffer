"""Какой Telegram слышит процесс: боевой или тестовая среда.

Тестовая среда Telegram — отдельный мир со своими аккаунтами и ботами, где платежи звёздами
проверяются бесплатно. Бот в ней ходит на тот же хост, но по адресу `/bot<token>/test/<метод>`;
в aiogram для этого есть готовая константа `TEST`. Сессия собирается здесь, в ОДНОМ месте:
процессов с `Bot(...)` у нас три (бот, нотифаер, оповещения коллектора), и тест-режим,
включённый у одного, молча оставлял бы остальные в боевой среде.
"""

from __future__ import annotations

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST

from sniffer.config import Settings


def bot_session(settings: Settings) -> AiohttpSession | None:
    """Сессия для `Bot(session=...)`. `None` — боевая среда: библиотека возьмёт свою сама."""
    return AiohttpSession(api=TEST) if settings.telegram_test_environment else None
