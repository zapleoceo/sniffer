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

from sniffer.bot.cards import MAX_CARDS, render_cards
from sniffer.domain.passport import Passport
from sniffer.search.intake import QueryIntake
from sniffer.search.live import run_plan
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

    passport = await QueryIntake().parse(text)
    await message.answer(_accepted(passport))

    try:
        items = await _find(passport)
    except Exception:
        # Граница запроса: неожиданная ошибка внутри поиска не должна оставлять
        # клиента без ответа. Трейсбек уходит в лог целиком.
        log.exception("bot.search_failed", chat_id=message.chat.id)
        await message.answer(SEARCH_FAILED)
        return

    if not items:
        await message.answer(NOTHING_FOUND)
        return
    await message.answer(render_cards(items, limit=MAX_CARDS))


async def _find(passport: Passport) -> list[RawItem]:
    sources = sorted(registered_sources())
    plan = await SearchPlanner().plan(passport, sources)
    log.info("bot.plan", tasks=len(plan.tasks), fallback=plan.is_fallback, sources=plan.sources())
    return await run_plan(plan)


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
