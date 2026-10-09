"""Снятие публичного превью t.me/<username> - обычный веб-запрос, без юзербота.

Юзербот не общается и здесь не участвует вообще: это та же страница, которую видит
любой браузер. Ошибка сети, статус не 200, таймаут - это `unavailable`, то есть
незнание; повторов в цикле нет (следующая попытка - только следующий проход, и то
лишь если решит вызывающий).

Частота ограничена снаружи: `pace()` вставляет паузу между запросами, а число
запросов за проход режет вызывающий (`collector/preview_scan.py`).
"""

from __future__ import annotations

import httpx

from sniffer.domain.chat_preview import PreviewSnapshot, parse_preview, unavailable

PREVIEW_URL = "https://t.me/{username}"
TIMEOUT_S = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; SnifferBotPreview/1.0)"


async def fetch_preview(client: httpx.AsyncClient, username: str) -> PreviewSnapshot:
    """Один запрос, без ретраев. Любая сетевая беда - `unavailable`, не исключение."""
    try:
        response = await client.get(
            PREVIEW_URL.format(username=username),
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        return unavailable(type(exc).__name__)
    if response.status_code != 200:
        return unavailable(f"http {response.status_code}")
    return parse_preview(response.text)
