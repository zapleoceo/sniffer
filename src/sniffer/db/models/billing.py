"""Оплата звёздами: согласие с условиями и события подписки (011_stars_billing.sql)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import NOW, Base, BigIdMixin


class UserConsent(Base):
    """Клиент подтвердил, что прочёл условия, ДО покупки: версия текста и время.

    Составной ключ (клиент, документ, версия): повторное согласие с той же
    версией ничего не меняет, а новая версия условий — отдельная строка.
    """

    __tablename__ = "user_consents"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    doc: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class BillingEvent(BigIdMixin, Base):
    """Событие оплаты без идентификатора платежа: изменение подписки, обращение."""

    __tablename__ = "billing_events"
    __table_args__ = (
        Index("billing_events_user_kind_idx", "tg_user_id", "kind", sa_text("created_at DESC")),
    )

    # Только против повторной доставки: порядок событий по нему определять нельзя.
    update_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charge_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
