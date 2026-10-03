"""Точка входа диалога: сообщение или нажатие кнопки → `Conversation`.

Хендлер намеренно тонкий. Всё, что он делает сам, — достаёт из апдейта
клиента и текст, отдаёт их разговору и рисует его ответы кнопками. Ни разбора
запроса, ни выбора вопросов, ни знания об источниках здесь нет.
"""

from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from sniffer.bot import (
    billing_wording,
    more_cards,
    paging,
    query_menu,
    tab_flow,
    threads,
    topics,
    watch_flow,
    wording,
    wording_plan,
)
from sniffer.bot import voice as voice_input
from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.commands import looks_like_command
from sniffer.bot.conversation import Conversation, Reply, Send
from sniffer.bot.handlers.billing import show_confirmation
from sniffer.bot.keyboards import (
    AnswerCallback,
    FeedbackCallback,
    PageCallback,
    PlanCallback,
    RequestsCallback,
    main_menu,
    markup,
    request_actions,
    requests_markup,
    without_paging,
)
from sniffer.bot.quota import QuotaService
from sniffer.bot.quota_ledger import account_of, new_quota
from sniffer.bot.search_gate import Start, start_new_search
from sniffer.bot.store import Client, PassportStore
from sniffer.config import get_settings
from sniffer.domain.clarify import ClarificationPlanner
from sniffer.domain.dialogue import Feedback

log = structlog.get_logger(__name__)

router = Router(name="search")

# Текст приветствия — в `wording`: его правит владелец, и править его не должно требовать
# хендлера. Имя осталось здесь, потому что на него ссылаются тесты и другие модули.
GREETING = wording.GREETING

_conversation: Conversation | None = None


_quota: QuotaService | None = None


def quota() -> QuotaService:
    """Одна квота на процесс. Журнал в базе, а не в ней, как и состояние разговора."""
    global _quota
    if _quota is None:
        _quota = new_quota()
    return _quota


def _planner() -> ClarificationPlanner | None:
    """Вопросы по базе — только на собственном каталоге.

    Каждый шаг сужения повторяет поиск, чтобы пересчитать счёт. Это дёшево на SQL
    по `listings` и дорого на живом поиске (модель и обход источников на каждый
    вопрос), поэтому на других режимах планировщика нет.
    """
    return ClarificationPlanner() if get_settings().catalog_mode == "listings" else None


def conversation() -> Conversation:
    """Один разговор на процесс. Состояние всё равно в базе, а не в нём."""
    global _conversation
    if _conversation is None:
        _conversation = Conversation(
            PassportStore(),
            scoped_finder=CatalogFinder(),
            quota=quota(),
            planner=_planner(),
            search_limit=watch_flow.DbSearchLimit(),
        )
    return _conversation


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(GREETING, reply_markup=main_menu())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(wording.HELP)


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
    started = await start_new_search(
        message, client, conversation(), prefer_tab=client.thread_id is not None
    )
    if started is not Start.ARMED:
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer(threads.ASK_WHAT)
        return
    await conversation().on_text(client, query, _sender(message))
    await _sync_title(message, client)


@router.message(Command("plan"))
async def plan(message: Message) -> None:
    """Остаток карточек и дата обновления. Только чтение: ничего не списывает и не начинает."""
    client = _client(message)
    if client is None:  # pragma: no cover — сообщение без автора
        return
    standing = await quota().standing(await account_of(client))
    await message.answer(wording_plan.plan_text(standing, selling=get_settings().selling))


@router.message(F.text)
async def search(message: Message) -> None:
    client = _client(message)
    if client is None:
        return
    text = message.text or ""
    if looks_like_command(text):
        # Известные команды перехватили свои обработчики выше; сюда добралась неизвестная.
        # Поиск по слову «terms» вместо ответа «такой команды нет» (FLOW-17) — это дефект.
        await message.answer(wording_plan.UNKNOWN_COMMAND)
        return
    await conversation().on_text(client, text, _sender(message))
    await _sync_title(message, client)


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
    await _sync_title(message, client)


@router.callback_query(AnswerCallback.filter())
async def answer(callback: CallbackQuery, callback_data: AnswerCallback) -> None:
    """Ответ кнопкой на уточняющий вопрос."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        # Сообщение старше 48 часов Telegram отдаёт недоступным — отвечать не в что.
        return
    client = topics.client_of_callback(callback, message)
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
    client = topics.client_of_callback(callback, message)
    if not await query_menu.select(client, callback_data.root):
        return
    await conversation().on_feedback(client, kind, _sender(message))


def _client(message: Message) -> Client | None:
    if message.from_user is None:
        # Пост от имени канала: паспорт привязывать не к кому.
        return None
    return topics.client_of_message(message)


async def _sync_title(message: Message, client: Client) -> None:
    """В теме имя темы следует за названием поиска; без темы ничего не делает."""
    if client.thread_id is not None and message.bot is not None:
        await tab_flow.sync_title(message.bot, client)


def _sender(message: Message) -> Send:
    async def send(reply: Reply) -> None:
        await message.answer(reply.text, reply_markup=markup(reply))

    return send


@router.callback_query(PlanCallback.filter())
async def plan_action(callback: CallbackQuery, callback_data: PlanCallback, bot: Bot) -> None:
    """Кнопка «Подписка» под предложением: экран с цифрами, согласие и ссылка — `/subscription`."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message) or callback_data.action != "subscribe":
        return
    if not get_settings().selling:
        await message.answer(billing_wording.SOON)
        return
    await show_confirmation(message, bot, callback.from_user.id)


@router.callback_query(RequestsCallback.filter())
async def manage_request(callback: CallbackQuery, callback_data: RequestsCallback) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    client = topics.client_of_callback(callback, message)
    action, root = callback_data.action, callback_data.root
    if action == "list":
        await _show_requests(message, client)
        return
    if action == "new":
        # Кнопка делает ровно то же, что команда: поиск открывается следующим
        # сообщением. Второй путь с собственным поведением рассыпался бы первым.
        started = await start_new_search(
            message, client, conversation(), prefer_tab=client.thread_id is not None
        )
        if started is Start.ARMED:
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


@router.callback_query(PageCallback.filter())
async def more(callback: CallbackQuery, callback_data: PageCallback) -> None:
    """«Ещё N» и «Показать все N»: следующая страница снимка выдачи."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    snapshot = paging.SNAPSHOTS.get(callback_data.token)
    if snapshot is None or snapshot.owner != callback.from_user.id:
        # Чужой снимок и просроченный неотличимы для клиента, и объяснять первое не нужно.
        await callback.answer(paging.EXPIRED, show_alert=True)
        return
    client = Client(callback.from_user.id, callback.from_user.username)
    outcome = await more_cards.show_more(
        _sender(message),
        callback_data.token,
        snapshot,
        callback_data.action,
        callback_data.offset,
        quota=quota(),
        account=await account_of(client),
    )
    if outcome is more_cards.Result.STALE:
        await callback.answer(paging.ALREADY_SHOWN)
        return
    if outcome is more_cards.Result.FAILED:
        await callback.answer(paging.TRY_AGAIN, show_alert=True)
        return
    await callback.answer()
    # Кнопки продолжения под прежней страницей снимаем: новая страница несёт свои.
    # Остальные кнопки (обратная связь, подписка) остаются.
    try:
        await message.edit_reply_markup(reply_markup=without_paging(message.reply_markup))
    except TelegramBadRequest as exc:
        # Разметка уже снята или сообщение не даёт править: карточки показаны, это косметика.
        log.info("bot.page_markup_kept", error=str(exc))
