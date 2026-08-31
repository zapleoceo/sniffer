"""Разведка чатов: очередь кандидатов, отклонённые, журнал вступлений.

Отдельный модуль, а не три класса в `sources.py`, по той же границе, по которой
разделены сами источники: `telegram_groups` читает чаты, `telegram_discover` их
набирает. В DDL эти таблицы стоят в разделе «источники», но живут они по другому
поводу — не «что мы прочитали», а «как чат попал в реестр».

Числа и правила здесь не дублируются: ограничения вступления описаны один раз в
`sources/telegram_discover_reference.py` (они переписаны из CLAUDE.md), а эти
модели — только имена таблиц и колонок для запросов.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Text
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import FALSE, NOW, Base, BigIdMixin


class ChatCandidate(BigIdMixin, Base):
    """Кандидат в реестр: ссылка, которую отбор уже проверил, а вступление нет.

    Одноимённая запись есть и в слое источников — там это неизменяемый
    `dataclass` домена, здесь строка таблицы. Репозиторий переводит одно в
    другое; смешивать их в одном модуле нельзя, поэтому импортируются они под
    разными именами.
    """

    __tablename__ = "chat_candidates"
    __table_args__ = (
        # Партиальный индекс: очередь разбирается только по 'queued', а строки
        # в 'joining' в выборку не попадают вовсе.
        Index(
            "chat_candidates_queue_idx",
            "priority",
            "found_at",
            postgresql_where=sa_text("status = 'queued'"),
        ),
    )

    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    invite_hash: Mapped[str | None] = mapped_column(Text)
    found_in: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("100"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'queued'"))
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class ChatReject(Base):
    """Отклонённый кандидат. Первичный ключ — сам `key`, `id` тут не нужен.

    Единственная таблица схемы без `BIGSERIAL`: строка отвечает на вопрос «этот
    ключ уже отбраковали?», и второй ключ рядом с ним был бы только лишним
    индексом.
    """

    __tablename__ = "chat_rejects"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class ChatJoinEvent(BigIdMixin, Base):
    """Журнал вступлений — источник правды по лимитам из CLAUDE.md.

    Строка появляется ДО вступления (`kind='claimed'`) одной транзакцией с
    проверкой ворот. Оставшаяся после обрыва связи строка намеренно продолжает
    занимать слот: недосчитать одно вступление безопаснее, чем сделать
    четвёртое.
    """

    __tablename__ = "chat_join_events"
    __table_args__ = (
        Index("chat_join_events_recent_idx", sa_text("happened_at DESC")),
        # Догнать беззвучный режим там, где он не встал с первого раза.
        Index(
            "chat_join_events_unmuted_idx",
            "tg_id",
            postgresql_where=sa_text("kind = 'joined' AND NOT muted"),
        ),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'claimed'"))
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(Text)
    happened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    next_allowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=FALSE)
    mute_error: Mapped[str | None] = mapped_column(Text)
