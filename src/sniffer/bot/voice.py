"""Голосовой запрос: скачать, распознать, показать клиенту, что услышали.

Зачем вообще. Человек с телефона в Нячанге печатает вьетнамские названия и
цифры медленно и с ошибками — «моцокил», «обьем» из живого лога тому пример.
Голосом ту же фразу он скажет за три секунды.

Три правила, без которых голос вреднее пользы.

**Всегда показываем, что услышали.** Распознавание ошибается, и клиент обязан
видеть текст, по которому пошёл поиск: иначе он получает непонятную выдачу и не
может догадаться, что бот услышал «сто» вместо «двести». Это же даёт ему
возможность поправить одним сообщением.

**Отказ распознавания — не отказ бота.** Нет права у ключа, брокер лёг, формат
не понят — клиент слышит «напишите текстом», а не молчание и не трейсбек.

**Длина ограничена.** Минута голоса — это уже не запрос, а рассказ; распознавать
её дорого, а искать всё равно будем по одной фразе. Ограничение стоит ДО
скачивания: файл на десять минут незачем даже тянуть.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from sniffer.broker.client import BrokerClient, BrokerError
from sniffer.broker.usage import default_usage_sink

log = structlog.get_logger(__name__)

# Дольше минуты — это уже не поисковый запрос. Порог щедрый: обычная фраза
# «ищу скутер автомат до четырёхсот долларов» укладывается в пять секунд.
MAX_VOICE_SECONDS = 60
# Двадцать мегабайт — потолок скачивания у Bot API; свой ставим ниже, потому
# что минута голоса весит около сотни килобайт, а всё, что сильно больше, —
# не голос.
MAX_VOICE_BYTES = 2 * 1024 * 1024

TOO_LONG = (
    "Голосовое длиннее минуты я не разберу. Скажите короче — "
    "«ищу скутер автомат до четырёхсот долларов» — или напишите текстом."
)
NOT_RECOGNISED = "Не разобрал голосовое. Напишите текстом, пожалуйста."
HEARD = "Услышал: «{text}»"

Download = Callable[[], Awaitable[bytes | None]]


async def transcribe(
    *,
    duration: int,
    download: Download,
    broker: BrokerClient | None = None,
) -> str | None:
    """Текст из голосового либо `None`, если разобрать не вышло.

    Скачивание передаётся функцией: качает его aiogram, а этот модуль о
    Telegram знать не должен — иначе распознавание нельзя проверить без бота.
    """
    if duration > MAX_VOICE_SECONDS:
        log.info("voice.too_long", duration=duration)
        return None

    audio = await _downloaded(download)
    if audio is None:
        return None

    own = broker is None
    client = broker or BrokerClient(usage=default_usage_sink)
    try:
        text = await client.transcribe(audio)
    except (BrokerError, OSError, TimeoutError) as exc:
        # Самая частая причина здесь — не поломка, а отсутствие права
        # `llm:audio` у ключа проекта. Клиенту разницы нет: он слышит «напишите
        # текстом», а причина уходит в лог владельцу.
        log.warning("voice.not_transcribed", kind=type(exc).__name__, error=str(exc)[:200])
        return None
    finally:
        if own:
            await client.aclose()

    # Обрезаем здесь, хотя клиент брокера обрезает тоже. Это не дублирование
    # знания, а граница доверия: «пустой транскрипт» — свойство ЭТОГО модуля, и
    # проверять его чужой реализацией значит зависеть от неё. Поймано тестом:
    # заглушка вернула пробелы, и поиск ушёл по пустоте.
    heard = text.strip()
    if not heard:
        log.info("voice.empty_transcript")
        return None
    log.info("voice.transcribed", chars=len(heard))
    return heard


async def _downloaded(download: Download) -> bytes | None:
    try:
        audio = await download()
    except Exception as exc:
        # Файл старше суток Telegram отдавать перестаёт, и это не наша поломка.
        log.warning("voice.not_downloaded", kind=type(exc).__name__, error=str(exc)[:200])
        return None
    if audio is None or not audio:
        return None
    if len(audio) > MAX_VOICE_BYTES:
        log.info("voice.too_big", size=len(audio))
        return None
    return audio
