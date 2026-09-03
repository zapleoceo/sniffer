"""Точка входа диалога: сообщение или нажатие кнопки → `Conversation`.

Хендлер намеренно тонкий. Всё, что он делает сам, — достаёт из апдейта
клиента и текст, отдаёт их разговору и рисует его ответы кнопками. Ни разбора
запроса, ни выбора вопросов, ни знания об источниках здесь нет.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from sniffer.bot import billing, query_menu, subscription
from sniffer.bot import voice as voice_input
from sniffer.bot.conversation import NO_REQUEST_YET, Conversation, Reply, Send
from sniffer.bot.keyboards import (
    AnswerCallback,
    FeedbackCallback,
    RequestsCallback,
    SubscribeCallback,
    markup,
    request_actions,
    requests_markup,
)
from sniffer.bot.store import Client, PassportStore
from sniffer.domain.dialogue import Feedback
from sniffer.domain.records import QueryOverview

log = structlog.get_logger(__name__)

router = Router(name="search")

GREETING = (
    "Я ищу частные объявления по чатам и доскам Вьетнама и приношу ссылки на оригиналы.\n\n"
    "Напишите словами, что нужно: <i>ищу скутер в Нячанге до 400 долларов</i> "
    "или <i>сниму квартиру в Нячанге до 10 млн донгов</i>.\n\n"
    "Если чего-то важного не хватает, уточню парой вопросов — отвечать можно кнопкой "
    "или словами. Объявление не перепечатываю: даю ссылку на источник и честно помечаю, "
    "если лот старый и мог быть продан.\n\n"
    "Несколько поисков и их мониторинги: /requests"
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


@router.message(Command("requests"))
async def requests(message: Message) -> None:
    client = _client(message)
    if client is not None:
        await _show_requests(message, client)


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


# ── подписка за звёзды ──────────────────────────────────────────────────────


@router.callback_query(SubscribeCallback.filter())
async def subscribe(callback: CallbackQuery, callback_data: SubscribeCallback) -> None:
    """«Следить за новыми» → счёт на одну звезду в месяц.

    Корень едет в кнопке: при нескольких запросах старая карточка обязана
    включать слежение именно за собой, а не за выбранным позже запросом.
    """
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        # Сообщение старше 48 часов Telegram отдаёт недоступным.
        return

    tg_user_id = callback.from_user.id
    root = callback_data.root
    if root <= 0 or not await subscription.owns(tg_user_id, root):
        await message.answer(NO_REQUEST_YET)
        return

    active = await subscription.active_for(tg_user_id, root)
    if active is not None and active.expires_at is not None:
        # Второй раз одно и то же не продаём.
        await message.answer(billing.ALREADY.format(until=active.expires_at.strftime("%d.%m.%Y")))
        return

    await message.answer_invoice(
        title=billing.TITLE,
        description=billing.DESCRIPTION,
        payload=billing.payload_for(root),
        currency=billing.SUBSCRIPTION_CURRENCY,
        prices=[LabeledPrice(label=billing.LABEL, amount=billing.SUBSCRIPTION_STARS)],
        subscription_period=billing.SUBSCRIPTION_PERIOD_S,
        # Пустая строка — так Telegram требует для звёзд: внешнего провайдера
        # нет, и токена у него взять негде.
        provider_token="",
    )


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
    items = await query_menu.list_for(client)
    item = next((row for row in items if row.root == root), None)
    if item is None:
        await message.answer("Этот запрос не найден. Откройте список заново.")
        return
    if action == "open":
        await query_menu.select(client, root)
    elif action == "search":
        await conversation().repeat(client, root, _sender(message))
        return
    elif action == "edit":
        await query_menu.select(client, root, editing=True)
        await message.answer(
            f"Изменяем: <b>{query_menu.title(item.passport)}</b>\n\n"
            "Напишите, что изменить, например «до 500», или новую формулировку целиком."
        )
        return
    elif action in {"pause", "resume"}:
        enabled = action == "resume"
        if not await query_menu.toggle(client, root, active=enabled):
            await message.answer("Мониторинг уже закончился. Его можно подключить заново.")
        items = await query_menu.list_for(client)
        item = next(row for row in items if row.root == root)
    else:
        return
    await message.answer(_request_text(item), reply_markup=request_actions(item))


async def _show_requests(message: Message, client: Client) -> None:
    items = await query_menu.list_for(client)
    if not items:
        await message.answer("Запросов пока нет. Напишите, что хотите найти.")
        return
    await message.answer(
        "Ваши запросы\n\n🟢 мониторинг работает · ⏸ на паузе · ▫️ без мониторинга",
        reply_markup=requests_markup(items),
    )


def _request_text(item: QueryOverview) -> str:
    states = {
        "active": "мониторинг работает",
        "paused": "мониторинг на паузе",
        "expired": "мониторинг закончился",
        "off": "мониторинг не подключён",
    }
    return f"<b>{query_menu.title(item.passport)}</b>\n{states[item.monitoring]}"


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Последняя точка, где отказ ничего не стоит клиенту.

    Отвечать обязаны за 10 секунд, иначе Telegram отменяет платёж, — поэтому
    здесь только разбор строки и одна проверка владельца. Ни поиска, ни модели,
    ни сети к источникам.

    Проверяем именно принадлежность цепочки: `payload` формируем мы, но
    приходит он от Telegram и доверенным не является.
    """
    root = billing.passport_root_from(query.invoice_payload)
    if root is None or not await subscription.owns(query.from_user.id, root):
        log.warning("billing.foreign_payload", payload=query.invoice_payload[:64])
        await query.answer(ok=False, error_message=billing.PAYLOAD_REFUSED)
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    """Деньги сняты. Отказывать уже нельзя — можно только включить подписку.

    Апдейт приходит ПОВТОРНО, если бот не ответил вовремя, поэтому зачисление
    идемпотентно по `telegram_payment_charge_id`. Повтор молчит: второе
    «подписка включена» на один платёж выглядит как двойное списание.
    """
    payment = message.successful_payment
    if payment is None or message.from_user is None:  # pragma: no cover — фильтр выше
        return
    purchase = billing.purchase_from(
        user_id=message.from_user.id,
        payload=payment.invoice_payload,
        charge_id=payment.telegram_payment_charge_id,
        amount=payment.total_amount,
        expiration=payment.subscription_expiration_date,
        is_recurring=bool(payment.is_recurring),
    )
    if purchase is None:
        # Оплатили счёт не нашего формата. Деньги уже сняты, поэтому молчать
        # нельзя: пусть человек напишет владельцу, а не гадает.
        log.error("billing.unknown_payload", payload=payment.invoice_payload[:64])
        await message.answer(billing.PAYMENT_STRANDED)
        return

    state = await subscription.activate(message.from_user.id, purchase)
    if state is None:
        return
    await message.answer(billing.THANKS.format(max_per_day=state.max_per_day))
