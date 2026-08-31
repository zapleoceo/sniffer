"""Репозитории — по одному на агрегат."""

from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.jobs import JobRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.passports import PassportRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.db.repositories.users import UserRepository

__all__ = [
    "ChatRepository",
    "JobRepository",
    "ListingRepository",
    "PassportRepository",
    "RawMessageRepository",
    "Repository",
    "UserRepository",
]
