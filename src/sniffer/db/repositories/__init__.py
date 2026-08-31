"""Репозитории — по одному на агрегат."""

from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.broker_calls import BrokerCallRepository
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.dialog import DialogRepository
from sniffer.db.repositories.jobs import JobRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.passports import PassportRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.db.repositories.requests import ClientRequestRepository
from sniffer.db.repositories.stats import StatsRepository
from sniffer.db.repositories.telegram_sessions import TelegramSessionRepository
from sniffer.db.repositories.users import UserRepository

__all__ = [
    "BrokerCallRepository",
    "ChatRepository",
    "ClientRequestRepository",
    "DialogRepository",
    "JobRepository",
    "ListingRepository",
    "PassportRepository",
    "RawMessageRepository",
    "Repository",
    "StatsRepository",
    "TelegramSessionRepository",
    "UserRepository",
]
