"""Доступ к базе — единственное место в проекте, где есть SQL.

Слои сверху (`bot`, `search`, `matching`, `pipeline`, `verifier`) получают
отсюда доменные записи и не знают ни про SQLAlchemy, ни про имена колонок.
Схема живёт в `infra/sql/001_init.sql`, её ORM-зеркало — в `models.py`.
"""

from sniffer.db.engine import dispose_engine, get_engine, get_sessionmaker, session_scope
from sniffer.db.repositories import (
    ChatRepository,
    JobRepository,
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)

__all__ = [
    "ChatRepository",
    "JobRepository",
    "ListingRepository",
    "PassportRepository",
    "RawMessageRepository",
    "UserRepository",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
]
