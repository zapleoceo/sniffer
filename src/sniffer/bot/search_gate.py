"""Ворота нового поиска: один вход для кнопки «Новый поиск», `/new` и «➕» панели.

Предел поисков (бесплатно 1, платно 10) проверяется ЗДЕСЬ до того, как человека попросят
написать запрос, и ещё раз в `Conversation._open` — там, где поиск реально рождается
(в том числе по обычным словам, без `/new`). Первая проверка нужна ради человека: просить
описать поиск и потом отказать — худший порядок. Вторая — ради предела: путей к созданию
ветки больше, чем кнопок.

В теме Telegram `/new` и кнопка открывают НОВУЮ тему, а не взводят общий флаг: в теме,
привязанной к поиску, следующее сообщение всё равно ушло бы в этот же поиск.
"""

from __future__ import annotations

from enum import StrEnum

from aiogram.types import Message

from sniffer.bot import tab_flow, topics, watch_flow
from sniffer.bot import watch_panel as panel
from sniffer.bot.conversation import Conversation
from sniffer.bot.store import Client
from sniffer.bot.watch_button import limit_markup, replace_markup


class Start(StrEnum):
    REFUSED = "refused"  # предел: человеку уже ответили
    TAB = "tab"  # открыта новая тема: писать надо в ней
    ARMED = "armed"  # флаг взведён: следующее сообщение откроет поиск


async def start_new_search(
    message: Message, client: Client, talker: Conversation, *, prefer_tab: bool
) -> Start:
    allowed, view = await watch_flow.can_open_new(client)
    if not allowed and view is not None:
        # Единственный поиск (бесплатный аккаунт) можно заменить прямо здесь: «/new» без
        # этого только отказывал, а человек пришёл именно за другим предметом.
        only = view.items[0].root if view.used == 1 and len(view.items) == 1 else None
        markup = limit_markup() if only is None else replace_markup(only)
        await message.answer(panel.limit_text(view), reply_markup=markup)
        return Start.REFUSED
    if prefer_tab and topics.active() and message.bot is not None:
        if await tab_flow.create_blank(message.bot, client):
            await message.answer(panel.NEW_TAB.format(name=tab_flow.NEW_TOPIC_NAME))
            return Start.TAB
    await talker.start_new(client)
    return Start.ARMED
