"""Кнопка «Следить» и перенос слота: тонкий слой над `DbSlots`.

Решение «включить / подписка нужна / слоты заняты» принимает `domain/slots.decide_enable`,
здесь только достаём факты из апдейта и рисуем ответ. Деньги здесь не списываются:
если слота нет, человек попадает на экран подтверждения с ценой (`handlers/billing`), где
всё как раньше: условия, согласие, ссылка.

Роутер подключается ДО диалога: кнопки стоят под карточками, и порядок роутеров с
`F.text` их не касается, но единый порядок (платежи → слоты → диалог) не оставляет места
для «забыли переставить».
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from aiogram import Bot, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sniffer.bot import query_menu, threads
from sniffer.bot import slot_wording as words
from sniffer.bot.billing_slots import DbSlots
from sniffer.bot.handlers.billing import show_confirmation
from sniffer.bot.keyboards import SubscribeCallback
from sniffer.bot.store import Client
from sniffer.domain.slots import Outcome

router = Router(name="slots")


class SlotCallback(CallbackData, prefix="slot"):
    """Перенос слота: на какую ветку и с какой. Оба корня в кнопке — состояние не храним."""

    action: str
    root: int
    source: int = 0


MOVE = "move"
BUY = "buy"
# Сколько кнопок «перенести» показываем: слотов у человека обычно один-два.
MAX_MOVE_BUTTONS = 5


def slots_service() -> DbSlots:
    """Точка подмены в тестах: порты слотов — подделка."""
    return DbSlots()


def _now() -> datetime:
    return datetime.now(UTC)


async def _live_message(callback: CallbackQuery) -> Message | None:
    if isinstance(callback.message, Message):
        return callback.message
    await callback.answer(words.STALE, show_alert=True)
    return None


@router.callback_query(SubscribeCallback.filter())
async def follow_button(
    callback: CallbackQuery, callback_data: SubscribeCallback, bot: Bot
) -> None:
    """«Следить за новыми»: включить на слоте, предложить подписку или перенос слота."""
    message = await _live_message(callback)
    if message is None:
        return
    await callback.answer()
    client = Client(callback.from_user.id, callback.from_user.username)
    root = callback_data.root
    if root == 0 or await query_menu.get_one(client, root) is None:
        await message.answer(words.NOT_YOURS)
        return
    outcome = await slots_service().enable(client.tg_user_id, root, _now())
    if outcome is Outcome.ENABLE:
        await message.answer(words.ENABLED)
    elif outcome is Outcome.ALREADY_ON:
        await message.answer(words.ALREADY_ON)
    elif outcome is Outcome.NEEDS_SUBSCRIPTION:
        await message.answer(words.NEEDS_SUBSCRIPTION)
        await show_confirmation(message, bot, client.tg_user_id)
    else:
        await _offer_move(message, client, root)


async def _offer_move(message: Message, client: Client, root: int) -> None:
    """Слоты заняты: показать, с какого поиска можно перенести, и кнопку «добавить слот»."""
    held = await slots_service().holders(client.tg_user_id, _now())
    named: list[tuple[int, str]] = []
    for monitor in held[:MAX_MOVE_BUTTONS]:
        item = await query_menu.get_one(client, monitor.root)
        if item is not None:
            named.append((monitor.root, threads.title(item.passport)))
    rows = [
        [
            InlineKeyboardButton(
                text=words.move_label(name),
                callback_data=SlotCallback(action=MOVE, root=root, source=source).pack(),
            )
        ]
        for source, name in named
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=words.ADD_SLOT_LABEL, callback_data=SlotCallback(action=BUY, root=root).pack()
            )
        ]
    )
    await message.answer(
        words.no_free_slot([escape(name, quote=False) for _, name in named]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(SlotCallback.filter())
async def slot_action(callback: CallbackQuery, callback_data: SlotCallback, bot: Bot) -> None:
    message = await _live_message(callback)
    if message is None:
        return
    await callback.answer()
    if callback_data.action == BUY:
        await show_confirmation(message, bot, callback.from_user.id)
    elif callback_data.action == MOVE:
        moved = await slots_service().move(
            callback.from_user.id,
            to_root=callback_data.root,
            from_root=callback_data.source,
            now=_now(),
        )
        await message.answer(words.MOVED if moved else words.MOVE_FAILED)
    else:
        await message.answer(words.STALE)
