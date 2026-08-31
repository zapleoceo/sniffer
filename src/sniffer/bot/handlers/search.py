"""Точка входа диалога: сообщение или нажатие кнопки → `Conversation`.

Хендлер намеренно тонкий. Всё, что он делает сам, — достаёт из апдейта
клиента и текст, отдаёт их разговору и рисует его ответы кнопками. Ни разбора
запроса, ни выбора вопросов, ни знания об источниках здесь нет.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from sniffer.bot.conversation import Conversation, Reply, Send
from sniffer.bot.keyboards import AnswerCallback, FeedbackCallback, markup
from sniffer.bot.store import Client, PassportStore
from sniffer.domain.dialogue import Feedback

log = structlog.get_logger(__name__)

router = Router(name="search")

GREETING = (
    "Я ищу частные объявления по чатам и доскам Вьетнама и приношу ссылки на оригиналы.\n\n"
    "Напишите словами, что нужно: <i>ищу скутер в Нячанге до 400 долларов</i> "
    "или <i>сниму квартиру в Нячанге до 10 млн донгов</i>.\n\n"
    "Если чего-то важного не хватает, уточню парой вопросов — отвечать можно кнопкой "
    "или словами. Объявление не перепечатываю: даю ссылку на источник и честно помечаю, "
    "если лот старый и мог быть продан."
)

_conversation: Conversation | None = None


def conversation() -> Conversation:
    """Один разговор на процесс. Состояние всё равно в базе, а не в нём."""
    global _conversation
    if _conversation is None:
        _conversation = Conversation(PassportStore())
    return _conversation


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(GREETING)


@router.message(F.text)
async def search(message: Message) -> None:
    client = _client(message)
    if client is None:
        return
    await conversation().on_text(client, message.text or "", _sender(message))


@router.callback_query(AnswerCallback.filter())
async def answer(callback: CallbackQuery, callback_data: AnswerCallback) -> None:
    """Ответ кнопкой на уточняющий вопрос."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        # Сообщение старше 48 часов Telegram отдаёт недоступным — отвечать не в что.
        return
    await conversation().on_answer(
        Client(callback.from_user.id, callback.from_user.username),
        callback_data.code,
        callback_data.value,
        _sender(message),
    )


@router.callback_query(FeedbackCallback.filter())
async def feedback(callback: CallbackQuery, callback_data: FeedbackCallback) -> None:
    """Обратная связь на карточках: уточняет паспорт и перезапускает подбор."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        kind = Feedback(callback_data.kind)
    except ValueError:
        # Кнопка из старой версии бота: молча игнорировать честнее, чем падать.
        log.warning("bot.unknown_feedback", kind=callback_data.kind)
        return
    await conversation().on_feedback(
        Client(callback.from_user.id, callback.from_user.username), kind, _sender(message)
    )


def _client(message: Message) -> Client | None:
    if message.from_user is None:
        # Пост от имени канала: паспорт привязывать не к кому.
        return None
    return Client(message.from_user.id, message.from_user.username)


def _sender(message: Message) -> Send:
    async def send(reply: Reply) -> None:
        await message.answer(reply.text, reply_markup=markup(reply))

    return send
