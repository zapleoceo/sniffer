"""Точка входа диалога: сообщение или нажатие кнопки → `Conversation`.

Хендлер намеренно тонкий. Всё, что он делает сам, — достаёт из апдейта
клиента и текст, отдаёт их разговору и рисует его ответы кнопками. Ни разбора
запроса, ни выбора вопросов, ни знания об источниках здесь нет.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from sniffer.bot import query_menu, threads
from sniffer.bot import voice as voice_input
from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.conversation import Conversation, Reply, Send
from sniffer.bot.keyboards import (
    AnswerCallback,
    FeedbackCallback,
    RequestsCallback,
    markup,
    request_actions,
    requests_markup,
)
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
    "если лот старый и мог быть продан.\n\n"
    "Ищете несколько разных вещей? Начинайте каждую с /new — поиски не перепутаются. "
    "Последние поиски и переключение между ними — /requests."
)

_conversation: Conversation | None = None


def conversation() -> Conversation:
    """Один разговор на процесс. Состояние всё равно в базе, а не в нём."""
    global _conversation
    if _conversation is None:
        _conversation = Conversation(PassportStore(), scoped_finder=CatalogFinder())
    return _conversation


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(GREETING)


@router.message(Command("requests"))
async def requests(message: Message) -> None:
    client = _client(message)
    if client is not None:
        await _show_requests(message, client)


@router.message(Command("new"))
async def new_request(message: Message, command: CommandObject) -> None:
    """Явный поиск. `/new скутер в Нячанге` — сразу, `/new` — следующим сообщением.

    Текст в той же команде существует не для скорости: голосовой запрос в
    команду не положишь, поэтому взведённый флаг нужен всё равно — и пусть у
    одного и того же «начни новый поиск» будет один вход, а не два похожих.

    Текст берётся у фильтра (`command.args`), а не режется здесь по пробелу:
    фильтр читает и подпись к фото, и перевод строки после команды, а
    `message.text.partition(" ")` терял и то и другое — «/new⏎квартира» уходила в
    поиск без предмета, а «/new скутер» в подписи к фото не искала ничего.
    """
    client = _client(message)
    if client is None:  # pragma: no cover — сообщение без автора
        return
    await conversation().start_new(client)
    query = (command.args or "").strip()
    if not query:
        await message.answer(threads.ASK_WHAT)
        return
    await conversation().on_text(client, query, _sender(message))


@router.message(F.text)
async def search(message: Message) -> None:
    client = _client(message)
    if client is None:
        return
    await conversation().on_text(client, message.text or "", _sender(message))


@router.message(F.voice)
async def voice(message: Message) -> None:
    """Голосовой запрос. Распознаём и дальше идём обычным текстовым путём.

    Услышанное показываем всегда: распознавание ошибается, и клиент обязан
    видеть, по какой фразе пошёл поиск, — иначе непонятную выдачу не с чем
    сопоставить, и поправить её нечем.
    """
    client = _client(message)
    if client is None or message.voice is None:  # pragma: no cover — фильтр выше
        return

    if message.voice.duration > voice_input.MAX_VOICE_SECONDS:
        await message.answer(voice_input.TOO_LONG)
        return

    file_id = message.voice.file_id

    async def download() -> bytes | None:
        stream = await message.bot.download(file_id) if message.bot else None
        return None if stream is None else stream.read()

    text = await voice_input.transcribe(duration=message.voice.duration, download=download)
    if not text:
        await message.answer(voice_input.NOT_RECOGNISED)
        return

    await message.answer(voice_input.HEARD.format(text=text))
    await conversation().on_text(client, text, _sender(message))


@router.callback_query(AnswerCallback.filter())
async def answer(callback: CallbackQuery, callback_data: AnswerCallback) -> None:
    """Ответ кнопкой на уточняющий вопрос."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        # Сообщение старше 48 часов Telegram отдаёт недоступным — отвечать не в что.
        return
    client = Client(callback.from_user.id, callback.from_user.username)
    if not await query_menu.select(client, callback_data.root):
        return
    await conversation().on_answer(
        client,
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
    client = Client(callback.from_user.id, callback.from_user.username)
    if not await query_menu.select(client, callback_data.root):
        return
    await conversation().on_feedback(client, kind, _sender(message))


def _client(message: Message) -> Client | None:
    if message.from_user is None:
        # Пост от имени канала: паспорт привязывать не к кому.
        return None
    return Client(message.from_user.id, message.from_user.username)


def _sender(message: Message) -> Send:
    async def send(reply: Reply) -> None:
        await message.answer(reply.text, reply_markup=markup(reply))

    return send


@router.callback_query(RequestsCallback.filter())
async def manage_request(callback: CallbackQuery, callback_data: RequestsCallback) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    client = Client(callback.from_user.id, callback.from_user.username)
    action, root = callback_data.action, callback_data.root
    if action == "list":
        await _show_requests(message, client)
        return
    if action == "new":
        # Кнопка делает ровно то же, что команда: поиск открывается следующим
        # сообщением. Второй путь с собственным поведением рассыпался бы первым.
        await conversation().start_new(client)
        await message.answer(threads.ASK_WHAT)
        return
    # Принадлежность — по самому поиску, а не по вхождению в список из пяти:
    # вытесненный из списка поиск остаётся поиском клиента, и его пауза, «Искать
    # снова» и «Изменить» обязаны работать так же, как у видимого.
    item = await query_menu.get_one(client, root)
    if item is None:
        await message.answer(threads.NOT_FOUND)
        return
    if action == "open":
        await query_menu.select(client, root)
    elif action == "search":
        await conversation().repeat(client, root, _sender(message))
        return
    elif action == "edit":
        await query_menu.select(client, root, editing=True)
        await message.answer(threads.edit_prompt(item.passport))
        return
    elif action in {"pause", "resume"}:
        if not await query_menu.toggle(client, root, active=action == "resume"):
            await message.answer(threads.MONITORING_ENDED)
        item = await query_menu.get_one(client, root) or item
    else:
        return
    await message.answer(threads.card_text(item), reply_markup=request_actions(item))


async def _show_requests(message: Message, client: Client) -> None:
    menu = await query_menu.list_for(client)
    if not menu.items:
        await message.answer(threads.NO_SEARCHES)
        return
    await message.answer(
        threads.list_text(starting_new=menu.starting_new),
        # «✓» значит «следующее сообщение уточнит этот поиск». Пока взведён
        # `/new`, это неправда, и отметки нет.
        reply_markup=requests_markup(menu.items, marked=not menu.starting_new),
    )
