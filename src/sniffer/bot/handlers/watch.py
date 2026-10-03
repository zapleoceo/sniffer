"""Панель «Мои слежения» и карточка фильтра: нажатия кнопок → `watch_flow` / `filter_flow`.

Хендлер тонкий: достаёт клиента, спрашивает флоу, рисует результат в том же сообщении
(`edit_text`), чтобы карточка правилась на месте, а не росла лентой.
"""

from __future__ import annotations

import structlog
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sniffer.bot import filter_card as card
from sniffer.bot import filter_flow, query_menu, tab_flow, threads, topics, watch_flow
from sniffer.bot import watch_panel as panel
from sniffer.bot.search_gate import Start, start_new_search
from sniffer.bot.store import Client
from sniffer.domain.field_spec import spec_by_key
from sniffer.domain.passport_edit import Change
from sniffer.domain.records import QueryOverview

log = structlog.get_logger(__name__)
router = Router(name="watch")
FREE = {"off", "expired"}


@router.message(Command("watch"))
async def watch(message: Message) -> None:
    client = topics.client_of_message(message)
    if client is not None:
        await _panel(message, client, edit=False)


@router.callback_query(panel.WatchCallback.filter())
async def on_watch(callback: CallbackQuery, callback_data: panel.WatchCallback) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    client = topics.client_of_callback(callback, message)
    action, root = callback_data.a, callback_data.root
    if action == panel.LIST:
        await _panel(message, client)
    elif action == panel.NEW:
        await _new_search(message, client)
    else:
        await _on_search(message, client, action, root, callback_data.to)


async def _on_search(message: Message, client: Client, action: str, root: int, target: int) -> None:
    item = await watch_flow.overview(client, root)
    if item is None:
        await message.answer(threads.NOT_FOUND)
        return
    if action in {panel.PAUSE, panel.RESUME}:
        if not await query_menu.toggle(client, root, active=action == panel.RESUME):
            await message.answer(threads.MONITORING_ENDED)
    elif action == panel.TAB:
        await _open_tab(message, client, root)
        return
    elif action == panel.MOVE:
        await _choose_target(message, client, item)
        return
    elif action == panel.MOVE_TO:
        await _move(message, client, root, target)
    elif action == panel.DELETE:
        await _show(message, panel.delete_text(item), panel.delete_markup(root))
        return
    elif action == panel.DELETE_OK:
        if await watch_flow.archive(client, root):
            await _panel(message, client, note=panel.DELETED)
        return
    await _card(message, client, root)


async def _choose_target(message: Message, client: Client, item: QueryOverview) -> None:
    view = await watch_flow.panel(client)
    others = [] if view is None else view.items
    free = [o for o in others if o.root != item.root and o.monitoring in FREE]
    text = "Куда перенести слот?" if free else panel.NO_FREE_TARGET
    await _show(message, text, panel.move_markup(item, others))


async def _move(message: Message, client: Client, root: int, target: int) -> None:
    moved = await watch_flow.move_slot(client, root, target)
    await message.answer(panel.MOVED if moved else panel.MOVE_REFUSED)


async def _new_search(message: Message, client: Client) -> None:
    from sniffer.bot.handlers import search

    started = await start_new_search(message, client, search.conversation(), prefer_tab=True)
    if started is Start.ARMED:
        await message.answer(threads.ASK_WHAT)


async def _open_tab(message: Message, client: Client, root: int) -> None:
    name = None if message.bot is None else await tab_flow.open_tab(message.bot, client, root)
    await message.answer(panel.TAB_FAILED if name is None else panel.TAB_OPENED.format(name=name))


async def _panel(message: Message, client: Client, *, edit: bool = True, note: str = "") -> None:
    view = await watch_flow.panel(client)
    if view is None:  # pragma: no cover — клиент без id
        return
    text = f"{note}\n\n{panel.panel_text(view)}" if note else panel.panel_text(view)
    markup = panel.panel_markup(view)
    if edit:
        await _show(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(card.FilterCallback.filter())
async def on_filter(callback: CallbackQuery, callback_data: card.FilterCallback) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    client = topics.client_of_callback(callback, message)
    root, action = callback_data.root, callback_data.a
    view = await filter_flow.open_card(client, root)
    if view is None:
        await message.answer(threads.NOT_FOUND)
        return
    spec = spec_by_key(callback_data.f) if callback_data.f else None
    if action == card.PICK:
        await _show(message, "Какое условие добавить?", card.addable_markup(view))
    elif spec is None or action == card.BACK:
        await _card(message, client, root)
    elif action in {card.SET, card.CLEAR}:
        change = Change(spec.key)
        if action == card.SET:
            change = Change(spec.key, card.parse_option(spec, callback_data.o))
        outcome = await filter_flow.edit(client, root, callback_data.v, [change])
        await _card(message, client, root, note=outcome.note)
    elif card.is_choice(spec):
        await _show(message, card.field_text(spec), card.field_markup(view, spec))
    else:
        await filter_flow.start_text_edit(client, root)
        await message.answer(card.prompt_text(spec))


async def _card(message: Message, client: Client, root: int, *, note: str | None = None) -> None:
    view = await filter_flow.open_card(client, root)
    if view is None:
        await message.answer(threads.NOT_FOUND)
        return
    item = QueryOverview(root=root, passport=view.passport, monitoring=view.monitoring)
    text = card.card_text(view) + (f"\n\n{note}" if note else "")
    offer = topics.active() and not await watch_flow.has_open_tab(client, root)
    footer = panel.card_footer(item, offer_tab=offer)
    await _show(message, text, card.card_markup(view, footer=footer))


async def _show(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Правит сообщение на месте; «не изменилось» — не ошибка: человек нажал то же самое."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
        log.debug("watch.not_modified")
