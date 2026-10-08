"""Состояние диалога живёт в базе, а не в памяти процесса.

Бот перезапускается на каждом деплое, а уточняющий вопрос висит между двумя
сообщениями клиента. Держи мы «о чём спросили» в словаре процесса — каждый
деплой стирал бы начатые разговоры, и клиент получал бы «Что ищем?» второй раз
подряд.

FSM aiogram здесь не используется намеренно: его хранилище пришлось бы писать
поверх той же таблицы, а состояние всё равно обязано лежать в
`passport_events` — паспорт ведёт эту историю и без диалога. Два хранилища
одного и того же расходятся, одно — нет.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.tabs import TabRepository
from sniffer.domain.dialogue import EVENT_USER_MESSAGE, DialogueState, advance, replay
from sniffer.domain.passport import Passport
from sniffer.domain.records import QueryOverview, StoredPassport

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class Client:
    """Кто пишет. Всё, что боту нужно знать о человеке до записи в базу."""

    tg_user_id: int
    username: str | None = None
    # Тема Telegram, из которой пришло сообщение. `None` — General, клиент без тем или
    # выключенный `TOPICS_ENABLED`: путь без тем остаётся прежним, как есть.
    thread_id: int | None = None


@dataclass(frozen=True, slots=True)
class Dialogue:
    """Текущий разговор: чей, о чём и на каком вопросе остановились.

    «О чём» — это всегда ОДНА ветка (поиск), активная. Паспорта соседних веток
    сюда не попадают вовсе, и это не упрощение, а граница: пока в разговоре лежит
    один паспорт, ни одна эвристика не может увести уточнение в чужую ветку —
    сравнивать ей просто не с чем.
    """

    user_id: int
    passport: StoredPassport | None = None
    state: DialogueState = field(default_factory=DialogueState)
    editing: bool = False
    # Человек сказал `/new` и ещё не написал, что ищет. Пока флаг взведён,
    # следующее сообщение открывает ветку, а не уточняет активную. Это СНИМОК на
    # момент `load`: настоящий флаг в базе, и тратится он там же (`start_requested`).
    starting_new: bool = False
    # Тема, в которой идёт разговор. Поиск ведёт тема, а не общий указатель клиента.
    thread_id: int | None = None


class DialogueStore(Protocol):
    """Чем диалог обменивается с хранилищем.

    Протокол, а не класс: тесты подставляют словарь и проверяют ход диалога
    без Postgres, а живой бот — реальные таблицы.
    """

    async def load(self, client: Client) -> Dialogue: ...

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue: ...

    async def start_requested(self, dialogue: Dialogue, passport: Passport) -> Dialogue | None:
        """Новая ветка по `/new`: флаг тратится В ТОМ ЖЕ действии, что и создание.

        `None` — флаг уже потрачен другим сообщением, и ветка ему не нужна. Именно
        это отличает метод от `start`: тот создаёт безусловно.
        """
        ...

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue: ...

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue: ...

    async def select(self, dialogue: Dialogue, root: int, *, editing: bool = False) -> Dialogue: ...

    async def live_threads(self, dialogue: Dialogue) -> list[QueryOverview]: ...

    async def await_new(self, dialogue: Dialogue) -> None: ...


class PassportStore:
    """`DialogueStore` поверх таблиц `passports` и `passport_events`."""

    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def load(self, client: Client) -> Dialogue:
        async with self._sessions() as session:
            user = await UserRepository(session).get_or_create(
                client.tg_user_id, username=client.username
            )
            await session.commit()
            if user.id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
                raise LookupError(f"клиент {client.tg_user_id} без id")

            if client.thread_id is not None:
                return await self._load_in_thread(
                    session,
                    user.id,
                    user.editing_passport_root,
                    client.thread_id,
                    starting_new=user.awaiting_new_request,
                )
            passports = PassportRepository(session)
            current = await passports.get_current(user.id)
            if current is None:
                return Dialogue(user_id=user.id)
            events = await passports.list_events(current.root)
            return Dialogue(
                user_id=user.id,
                passport=current,
                state=replay(events),
                editing=user.editing_passport_root == current.root,
                starting_new=user.awaiting_new_request,
            )

    async def _load_in_thread(
        self,
        session: AsyncSession,
        user_id: int,
        editing_root: int | None,
        thread_id: int,
        *,
        starting_new: bool = False,
    ) -> Dialogue:
        """Разговор в теме: корень берётся из связи, а не из общего указателя клиента.

        Темы без связи (человек создал её сам) читаются как пустой разговор: первое сообщение
        откроет в ней новый поиск и привяжет тему (`start`).
        """
        root = await TabRepository(session).root_of(user_id, thread_id)
        passports = PassportRepository(session)
        current = None if root is None else await passports.current_of(user_id, root)
        if current is None:
            return Dialogue(user_id=user_id, thread_id=thread_id)
        return Dialogue(
            user_id=user_id,
            passport=current,
            state=replay(await passports.list_events(current.root)),
            editing=editing_root == current.root,
            starting_new=starting_new,
            thread_id=thread_id,
        )

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue:
        """Новая формулировка — новая цепочка версий и чистый счётчик вопросов.

        В теме поиск и связь «тема — корень» пишутся одной транзакцией. Проигравший гонки двух
        первых сообщений в новой теме ничего не вставляет (сессия закрывается без коммита) и
        продолжает разговор победителя: две ветки в одной теме были бы тем самым дефектом,
        который `UNIQUE (user_id, message_thread_id)` и призван не пускать.
        """
        async with self._sessions() as session:
            thread = dialogue.thread_id
            stored = await self._insert(
                session, dialogue.user_id, passport, commit=False, move_pointer=thread is None
            )
            if thread is not None and not await TabRepository(session).claim(
                dialogue.user_id, stored.root, thread
            ):
                await session.rollback()
                return await self._load_in_thread(session, dialogue.user_id, None, thread)
            await session.commit()
        return Dialogue(
            user_id=dialogue.user_id,
            passport=stored,
            state=DialogueState(),
            thread_id=dialogue.thread_id,
        )

    async def start_requested(self, dialogue: Dialogue, passport: Passport) -> Dialogue | None:
        """`/new`: потратить флаг и создать ветку одной транзакцией.

        Порядок «сначала флаг, потом ветка» и общая транзакция — это и есть
        гарантия. Флаг, снятый ДО разбора, терялся бы при упавшем разборе; флаг,
        снятый отдельной транзакцией после создания, оставлял бы окно, в котором
        два сообщения открывают по ветке. Здесь проигравший не вставляет ничего:
        сессия закрывается без коммита.
        """
        async with self._sessions() as session:
            if not await PassportRepository(session).consume_new_request(dialogue.user_id):
                return None
            stored = await self._insert(session, dialogue.user_id, passport)
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def _insert(
        self,
        session: AsyncSession,
        user_id: int,
        passport: Passport,
        *,
        commit: bool = True,
        move_pointer: bool = True,
    ) -> StoredPassport:
        passports = PassportRepository(session)
        stored = await passports.save_new(user_id, passport, move_pointer=move_pointer)
        await passports.add_event(stored.id, EVENT_USER_MESSAGE, {"text": passport.raw_query})
        if commit:
            await session.commit()
        return stored

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue:
        """Правка поля — новая версия, а не перезапись (passport.md)."""
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("нечего уточнять: паспорта ещё нет")
        async with self._sessions() as session:
            passports = PassportRepository(session)
            stored = await passports.save_revision(
                dialogue.passport, passport, move_pointer=dialogue.thread_id is None
            )
            await passports.add_event(stored.id, kind, payload)
            await session.commit()
        return Dialogue(
            user_id=dialogue.user_id,
            passport=stored,
            state=advance(dialogue.state, kind, payload),
            thread_id=dialogue.thread_id,
        )

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue:
        """Событие без правки паспорта: заданный вопрос или пропуск «не важно»."""
        if dialogue.passport is None:  # pragma: no cover — вызывающий проверяет
            raise ValueError("событие без паспорта")
        async with self._sessions() as session:
            await PassportRepository(session).add_event(dialogue.passport.id, kind, payload)
            await session.commit()
        return replace(dialogue, state=advance(dialogue.state, kind, payload))

    async def live_threads(self, dialogue: Dialogue) -> list[QueryOverview]:
        """Ветки в списке, недавно использованные сверху. Нужны, чтобы знать, есть ли место."""
        async with self._sessions() as session:
            return await PassportRepository(session).list_queries(dialogue.user_id)

    async def await_new(self, dialogue: Dialogue) -> None:
        """`/new`: следующее сообщение открывает ветку, а не уточняет активную."""
        async with self._sessions() as session:
            await PassportRepository(session).await_new_request(dialogue.user_id)
            await session.commit()

    async def select(self, dialogue: Dialogue, root: int, *, editing: bool = False) -> Dialogue:
        """Переключить контекст только на принадлежащую клиенту цепочку."""
        async with self._sessions() as session:
            if dialogue.thread_id is not None and (
                await TabRepository(session).root_of(dialogue.user_id, dialogue.thread_id) != root
            ):
                return dialogue
            passports = PassportRepository(session)
            if not await passports.select(
                dialogue.user_id,
                root,
                editing=editing,
                move_pointer=dialogue.thread_id is None,
            ):
                return dialogue
            current = (
                await passports.current_of(dialogue.user_id, root)
                if dialogue.thread_id is not None
                else await passports.get_current(dialogue.user_id)
            )
            events = [] if current is None else await passports.list_events(current.root)
            await session.commit()
        return Dialogue(
            user_id=dialogue.user_id,
            passport=current,
            state=replay(events),
            editing=editing,
            thread_id=dialogue.thread_id,
        )
