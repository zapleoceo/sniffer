"""Добор архива чата — чтение ленты ВНИЗ, в отличие от догона свежего.

Зачем отдельно от `ingest.py`. Догон свежего отвечает на вопрос «что нового с
прошлого раза» и по определению короткий: пачка в 200 сообщений покрывает
пятнадцать минут любой барахолки. Архив — вопрос другой и по объёму, и по
осторожности: у `@auto_moto_vietnam` двадцать восемь тысяч сообщений, и
дочитать их значит сделать сотню запросов подряд с одного аккаунта.

Без этого модуля чат отдавал ровно те 200 сообщений, что висели в нём на
момент вступления. Замер 01.09.2026: два чата, 148 сообщений в сырье — при
том что в одном только первом чате их 28 113. Мерить на таком корпусе нечего,
а воронке, когда она появится, не на чем учиться.

Три правила безопасности, и каждое здесь не из осторожности вообще, а против
конкретного риска.

- **Один чат за проход.** Идти по десяти чатам сразу — это выкачка; по одному
  — человек, листающий ленту вверх.
- **Потолок страниц на проход.** Пять по 200 = тысяча сообщений раз в
  пятнадцать минут, то есть один запрос в три минуты в среднем. Медленнее, чем
  читает человек.
- **Любая ошибка останавливает проход, а курсор остаётся на последней
  удавшейся странице.** Отдельного разбора `FloodWaitError` здесь нет намеренно:
  перечень классов ошибок закрывает только то, что вспомнили, а «остановиться и
  прийти через пятнадцать минут» — правильный ответ на любую из них, включая ту,
  которой никто не назвал. Следующий проход всё равно не раньше чем через
  четверть часа, и этого хватает, чтобы пересидеть типичный флуд-лимит чтения.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import structlog

from sniffer.collector.ingest import HistoryReader, to_raw
from sniffer.domain.records import Chat, RawMessage

log = structlog.get_logger(__name__)

# Столько же, сколько у догона: это предел одной выдачи Telegram, а не наш
# выбор, и делать его меньше значит слать больше запросов за те же сообщения.
BACKFILL_PAGE = 200
BACKFILL_PAGES_PER_TICK = 5
# Пауза между страницами. Ровный интервал — подпись автомата, но здесь она
# нужна не для маскировки, а чтобы пять запросов не ушли одной очередью.
BACKFILL_PAUSE_S = 2.0


class BackfillStore(Protocol):
    async def next_backfill(self) -> Chat | None: ...

    async def store_archive(
        self, chat: Chat, messages: list[RawMessage], *, oldest_msg_id: int, done: bool
    ) -> int: ...


@dataclass(slots=True)
class HistoryBackfill:
    """Один проход добора: до пяти страниц архива одного чата."""

    reader: HistoryReader
    store: BackfillStore
    pages: int = BACKFILL_PAGES_PER_TICK
    page_size: int = BACKFILL_PAGE
    pause_s: float = BACKFILL_PAUSE_S

    async def run(self) -> int:
        chat = await self.store.next_backfill()
        if chat is None:
            return 0

        # Нетронутый чат начинаем от последнего известного сообщения. Первая
        # страница при этом перекроется с тем, что уже взял догон свежего, —
        # `ON CONFLICT DO NOTHING` её и погасит. Один перекрытый запрос на чат
        # за всю его жизнь дешевле, чем ещё одна колонка под «где начать».
        cursor = chat.backfill_msg_id or chat.last_msg_id
        if cursor <= 1:
            # Ни одного сообщения ещё не прочитано: добирать не от чего, догон
            # свежего сам поставит точку отсчёта на ближайшем проходе.
            return 0

        inserted = 0
        for page in range(self.pages):
            if page:
                await asyncio.sleep(self.pause_s)
            try:
                messages = await self.reader.history(
                    chat.username or chat.tg_id, limit=self.page_size, max_id=cursor
                )
            except Exception as exc:
                log.warning(
                    "collector.backfill_failed",
                    chat=chat.tg_id,
                    cursor=cursor,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return inserted

            if not messages:
                # Начало ленты. Больше в этот чат вниз не ходим никогда.
                await self.store.store_archive(chat, [], oldest_msg_id=cursor, done=True)
                log.info("collector.backfill_done", chat=chat.tg_id, oldest=cursor)
                return inserted

            oldest = min(message.id for message in messages)
            inserted += await self.store.store_archive(
                chat, to_raw(chat, messages), oldest_msg_id=oldest, done=False
            )
            cursor = oldest

        log.info(
            "collector.backfilled",
            chat=chat.tg_id,
            pages=self.pages,
            inserted=inserted,
            cursor=cursor,
        )
        return inserted
