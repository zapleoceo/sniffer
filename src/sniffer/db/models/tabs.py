"""Вкладки поиска: тема Telegram и пометка «архив».

ORM-зеркало `infra/sql/017_search_tabs.sql`. Корень поиска без внешнего ключа — как
у `subscriptions.passport_root`: корнем служит id первой версии цепочки.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import NOW, Base, BigIdMixin

OPEN = "open"
LOST = "lost"
ARCHIVED = "archived"


class SearchTab(BigIdMixin, Base):
    __tablename__ = "search_tabs"
    __table_args__ = (
        UniqueConstraint("user_id", "passport_root"),
        UniqueConstraint("user_id", "message_thread_id"),
        CheckConstraint("state IN ('open', 'lost', 'archived')", name="search_tabs_state_check"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    passport_root: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    shown_title: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
