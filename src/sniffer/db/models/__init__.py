"""ORM-зеркало `infra/sql/001_init.sql`.

Схему создаёт SQL-файл, а не `metadata.create_all`: DDL с расширениями,
GIN-индексами и `vector(1024)` в декларативных моделях выражается хуже, чем
обычным SQL, а два источника правды про схему — гарантированный дрейф. Модели
существуют ради запросов: имя колонки проверяется импортом, а не опечаткой в
строке.

Отсюда правило: изменение здесь идёт вместе с правкой DDL, а
`tests/test_db_models.py` сверяет имена таблиц и колонок прямо с SQL-файлом —
без базы, поэтому дрейф ловится на любой машине.

Разбиение по модулям повторяет разделы самого DDL: источники, разведка чатов,
предложения, клиенты и паспорта, подписки и доставка, очередь.
Разбиение по модулям повторяет разделы самого DDL: источники, предложения,
клиенты и паспорта, подписки и доставка, очередь, наблюдаемость и сессии.
"""

from sniffer.db.models.base import EMBEDDING_DIM, Base, BigIdMixin
from sniffer.db.models.clients import Passport, PassportEvent, User
from sniffer.db.models.delivery import Notification, Outbox, Payment, Subscription
from sniffer.db.models.discovery import ChatCandidate, ChatJoinEvent, ChatReject
from sniffer.db.models.jobs import Job
from sniffer.db.models.listings import Listing, ListingMedia
from sniffer.db.models.observability import (
    BrokerCall,
    ClientRequest,
    DialogMessage,
    TelegramSession,
)
from sniffer.db.models.sources import Chat, RawMessage, Seller

__all__ = [
    "EMBEDDING_DIM",
    "Base",
    "BigIdMixin",
    "BrokerCall",
    "Chat",
    "ChatCandidate",
    "ChatJoinEvent",
    "ChatReject",
    "ClientRequest",
    "DialogMessage",
    "Job",
    "Listing",
    "ListingMedia",
    "Notification",
    "Outbox",
    "Passport",
    "PassportEvent",
    "Payment",
    "RawMessage",
    "Seller",
    "Subscription",
    "TelegramSession",
    "User",
]
