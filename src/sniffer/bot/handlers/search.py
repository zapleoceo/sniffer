"""Первый рабочий диалог: свободный текст → паспорт → план → карточки.

Хендлер намеренно тонкий. Всё, что он делает сам, — переводит сообщение в
вызовы поиска и обратно в текст; ни разбора запроса, ни бюджета поиска, ни
знания об источниках здесь нет.

Клиента ведём подтверждением того, что мы поняли: поиск идёт десятки секунд, и
молчащий бот за это время успевает выглядеть сломанным.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from sniffer.bot import journal
from sniffer.bot.cards import MAX_CARDS, render_cards
from sniffer.broker.usage import request_scope
from sniffer.domain.passport import Passport
from sniffer.search.intake import QueryIntake
from sniffer.search.live import run_plan
from sniffer.search.plan import SearchPlan
from sniffer.search.planner import SearchPlanner
from sniffer.search.vocabulary import city_name
from sniffer.sources.base import RawItem, registered_sources

log = structlog.get_logger(__name__)

router = Router(name="search")

GREETING = (
    "Я ищу частные объявления по чатам и доскам Вьетнама и приношу ссылки на оригиналы.\n\n"
    "Напишите словами, что нужно: <i>ищу скутер в Нячанге до 400 долларов</i> "
    "или <i>сниму квартиру в Нячанге до 10 млн донгов</i>.\n\n"
    "Объявление не перепечатываю — даю ссылку на источник и честно помечаю, "
    "если лот старый и мог быть продан."
)
NOTHING_FOUND = (
    "По этому запросу ничего не нашлось. Попробуйте иначе: без марки, "
    "с другим бюджетом или другой формулировкой."
)
SEARCH_FAILED = "Не смог доискать: источники не ответили. Попробуйте ещё раз через пару минут."


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(GREETING)


@router.message(F.text)
async def search(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    opened = await _open(message, text)
    # Расходы на модель относятся к этому запросу и ни к какому другому:
    # contextvar доносит его id до клиента брокера через слои, которым он не
    # нужен, — и не путается между двумя клиентами, отвечающими одновременно.
    with request_scope(opened.request_id if opened else None):
        await _run(message, text, opened)


async def _run(message: Message, text: str, opened: journal.OpenRequest | None) -> None:
    watch = journal.Stopwatch()
    passport = await QueryIntake().parse(text)
    watch.lap("intake_ms")

    await _answer(message, opened, _accepted(passport))

    try:
        plan, items = await _find(passport, watch)
    except Exception as exc:
        # Граница запроса: неожиданная ошибка внутри поиска не должна оставлять
        # клиента без ответа. Трейсбек уходит в лог целиком.
        log.exception("bot.search_failed", chat_id=message.chat.id)
        await _answer(message, opened, SEARCH_FAILED)
        await journal.close_request(
            opened, stages=watch.stages, error=f"{type(exc).__name__}: {exc}"
        )
        return

    answer = NOTHING_FOUND if not items else render_cards(items, limit=MAX_CARDS)
    await _answer(message, opened, answer)
    await journal.close_request(
        opened,
        stages=watch.stages,
        result_count=len(items),
        plan_fallback=plan.is_fallback,
        sources=plan.sources(),
    )


async def _find(passport: Passport, watch: journal.Stopwatch) -> tuple[SearchPlan, list[RawItem]]:
    sources = sorted(registered_sources())
    plan = await SearchPlanner().plan(passport, sources)
    watch.lap("plan_ms")
    log.info("bot.plan", tasks=len(plan.tasks), fallback=plan.is_fallback, sources=plan.sources())
    items = await run_plan(plan)
    watch.lap("search_ms")
    return plan, items


async def _open(message: Message, text: str) -> journal.OpenRequest | None:
    """Открыть запрос в журнале. Анонимный апдейт журналировать некуда."""
    if message.from_user is None:
        return None
    return await journal.open_request(
        message.from_user.id, text, username=message.from_user.username
    )


async def _answer(message: Message, opened: journal.OpenRequest | None, text: str) -> None:
    """Ответить клиенту и записать ответ в журнал — в этом порядке.

    Сначала человек, потом лог: сломанный журнал не должен задерживать ответ, а
    порядок «сначала записать» ровно это и делал бы.
    """
    await message.answer(text)
    await journal.log_answer(opened, text)


def _accepted(passport: Passport) -> str:
    """Показываем, что поняли, — это дешевле уточняющего вопроса."""
    parts: list[str] = []
    if passport.category:
        parts.append(passport.category.value)
    city = city_name(passport.city, "ru")
    if city:
        parts.append(city)
    if passport.budget.max:
        currency = passport.budget.currency.value if passport.budget.currency else ""
        parts.append(f"до {passport.budget.max:g} {currency}".strip())
    understood = ", ".join(parts) if parts else "запрос как есть"
    return f"Понял: {understood}. Ищу, это занимает до минуты."
