"""Общие заготовки тестов поисков: разговор на правилах разбора и сборка паспорта.

Отдельным файлом, а не копией в каждом: четыре файла про поиски (`test_request_threads`,
`test_thread_notice`, `test_thread_race`, `test_thread_menu`) собирают один и тот же
разговор, и две копии заготовки разошлись бы на первой правке разбора.
"""

from __future__ import annotations

from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.store import Client
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.stubs import MemoryStore, SilentJournal

CLIENT = Client(tg_user_id=42, username="dima")


class Replies:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.sent.append(reply)

    @property
    def texts(self) -> list[str]:
        return [reply.text for reply in self.sent]


async def nothing(_passport: Passport) -> Found:
    return Found(items=[])


class Rules:
    """Разбор по правилам: категорию и город берём из слов клиента."""

    async def parse(self, text: str) -> Passport:
        return parse_query(text)


def talk(store: MemoryStore) -> Conversation:
    """Разговор на правилах разбора.

    Заранее заданный паспорт здесь не годится принципиально — он подменил бы
    ровно те поля, по которым ветки и различаются.
    """
    return Conversation(store, intake=lambda: Rules(), finder=nothing, recorder=SilentJournal())


def bike(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=400, currency=Currency.USD),
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]
