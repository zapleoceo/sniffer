"""Сессия юзербота: состояние наружу, шифртекст — только коллектору.

Два метода чтения не по недосмотру. `active_state()` отдаёт доменную запись
без секрета и годится дашборду; `active_session_string()` расшифровывает и
существует ровно для коллектора. Один метод «отдай всё» рано или поздно попал
бы в HTML.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from sniffer.crypto import decrypt, encrypt
from sniffer.db import models
from sniffer.db.mappers import to_session_state
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import SessionState

# Текст исключения Telethon может быть длинным; в колонку кладём начало —
# класс ошибки и номер стоят в первых символах.
MAX_ERROR_CHARS = 500


class TelegramSessionRepository(Repository):
    async def active_state(self) -> SessionState | None:
        row = await self._session.scalar(
            select(models.TelegramSession)
            .where(models.TelegramSession.is_active.is_(True))
            .order_by(models.TelegramSession.updated_at.desc(), models.TelegramSession.id.desc())
            .limit(1)
        )
        return to_session_state(row) if row is not None else None

    async def states(self) -> list[SessionState]:
        rows = await self._session.scalars(
            select(models.TelegramSession).order_by(models.TelegramSession.id)
        )
        return [to_session_state(row) for row in rows]

    async def active_session_string(self) -> str | None:
        """Расшифрованная строка сессии. Вызывается только коллектором."""
        row = await self._session.scalar(
            select(models.TelegramSession)
            .where(models.TelegramSession.is_active.is_(True))
            .order_by(models.TelegramSession.updated_at.desc(), models.TelegramSession.id.desc())
            .limit(1)
        )
        return decrypt(row.session_enc) if row is not None else None

    async def save(self, phone: str, session_string: str) -> SessionState:
        """Новая сессия для номера. Остальные номера деактивируются.

        Активной сессия может быть только одна: два процесса с разными
        сессиями одного аккаунта получают от Telegram `AuthKeyDuplicated` и
        роняют друг друга (architecture.md, раздел 3).
        """
        encrypted = encrypt(session_string)
        now = datetime.now(UTC)

        await self._session.execute(
            update(models.TelegramSession)
            .where(models.TelegramSession.phone != phone)
            .values(is_active=False, updated_at=now)
        )
        row = await self._session.scalar(
            select(models.TelegramSession).where(models.TelegramSession.phone == phone)
        )
        if row is None:
            row = models.TelegramSession(phone=phone, session_enc=encrypted, is_active=True)
            self._session.add(row)
        else:
            row.session_enc = encrypted
            row.is_active = True
            row.last_error = None
            row.last_error_at = None
        row.updated_at = now
        await self._session.flush()
        return to_session_state(row)

    async def mark_ok(self, phone: str) -> None:
        """Сессия только что успешно подключилась."""
        now = datetime.now(UTC)
        await self._session.execute(
            update(models.TelegramSession)
            .where(models.TelegramSession.phone == phone)
            .values(last_ok_at=now, last_error=None, last_error_at=None, updated_at=now)
        )

    async def mark_failed(self, phone: str, error: str) -> None:
        """Сессия отвалилась: снимаем активность и запоминаем причину.

        Активность снимаем сразу, а не после N попыток: коллектор с отозванной
        сессией не выздоравливает сам, а перебор чатов с мёртвым ключом — это
        путь к бану аккаунта.
        """
        now = datetime.now(UTC)
        await self._session.execute(
            update(models.TelegramSession)
            .where(models.TelegramSession.phone == phone)
            .values(
                is_active=False,
                last_error=error[:MAX_ERROR_CHARS],
                last_error_at=now,
                updated_at=now,
            )
        )
