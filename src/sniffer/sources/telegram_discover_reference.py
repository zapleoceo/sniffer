"""Контракт разведки чатов: числа, записи, протоколы зависимостей.

Отдельно от кода по той же причине, что и у `telegram_groups`: протоколы нужны
трём сторонам сразу — разведке, слою `db` и тестам, — а обратной зависимости
`db → sources` возникать не должно. Структурная типизация это обеспечивает:
реализация подставляется снаружи и не импортирует этот модуль.

Числа ниже не выведены из удобства и не подобраны экспериментом. Они переписаны
из CLAUDE.md, раздел «Работа с Telegram», где владелец 31.08.2026 открыл ровно
два исключения из правила «юзербот только читает» — вступление и беззвучный
режим — и сразу же обвесил первое ограничениями. Менять их здесь нельзя:
поднятый потолок не «ускорит разведку», а положит единственный аккаунт, на
котором держится продукт.

Ручного вступления больше нет (roadmap, волна 4.5): очередь — единственный
способ, каким проект набирает чаты. Поэтому лимиты здесь не советы, а ворота, и
устроены они так, чтобы держаться при перезапуске процесса и при двух воркерах
сразу — см. `JoinLedger`.

Разрешённая поверхность Telegram сведена к `TelegramJoiner`. Кроме чтения в ней
ровно два действия — `join_public` / `join_invite` и `set_muted`. Ни отправки,
ни реакции, ни отметки о прочтении в типе нет, и дописать их молча не выйдет:
mypy не пропустит.

Отдельно про `check_invite`: это `messages.checkChatInvite`, то есть **чтение**,
а не третье исключение. Оно ничего не отправляет, никому не адресовано и в чате
не видно — ровно как `resolve_username`, только для закрытой группы. Нужно оно
потому, что **выйти из чата нельзя**: такого действия в закрытом списке
CLAUDE.md нет, поэтому неверное вступление не откатывается ничем — оно навсегда
занимает место под потолком и стоит одного из трёх суточных слотов. Значит
единственное место, где ошибку ещё можно не совершить, — до вступления.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

SOURCE_NAME = "telegram_discover"

# CLAUDE.md: не больше 3 вступлений в сутки. Окно скользящее, а не календарное,
# и это осознанно строже буквы: календарные сутки разрешают три вступления в
# 23:59 и ещё три в 00:01, то есть шесть за две минуты — ровно тот всплеск, от
# которого правило и защищает.
MAX_JOINS_PER_DAY = 3
JOIN_WINDOW = timedelta(hours=24)

# CLAUDE.md: пауза между вступлениями не меньше часа, «вразнобой, не по
# таймеру». Ровно час в ровно час — это подпись автомата; разброс до получаса
# сверху делает ритм человеческим, оставаясь строже минимума.
MIN_JOIN_PAUSE = timedelta(hours=1)
JOIN_PAUSE_JITTER = timedelta(minutes=30)

# CLAUDE.md: «потолок общего числа чатов, при достижении — только замена, не
# рост». Числа владелец не назвал, поэтому берём его же список: волны 1–3 в
# docs/chats-nha-trang.md — 35 групп Нячанга, практически весь известный рынок.
# 50 — это он плюс запас на находки разведки. Дальше растёт не охват, а шум и
# риск: живой поиск всё равно обходит 10 чатов (spec-v2, 2.3).
MAX_TRACKED_CHATS = 50

# Сколько результатов брать из `contacts.SearchRequest` на одно слово словаря.
# Telegram отдаёт релевантное первым; хвост — это чаты про что угодно.
MAX_SEARCH_RESULTS = 30

# Приоритет находки разведки: хуже любого места в курируемом списке
# (docs/chats-nha-trang.md — волны 1..3 сидируются с 10, 20, 30). Очередь
# разбирается по три чата в сутки, и находка не имеет права оттеснить группу,
# которую владелец выбрал руками.
DISCOVERED_PRIORITY = 100

# Ранг в реестре `chats` — тем же соображением: живой поиск берёт первые
# десять чатов по рангу, у ручных он 100 и меньше.
DISCOVERED_RANK = 500

# Причины отказа. Строка живёт в журнале отклонённых и объясняет человеку,
# почему кандидат больше не всплывёт — «отклонён» без причины через месяц
# невозможно ни перепроверить, ни отменить.
REJECT_CHANNEL = "channel"
REJECT_BOT = "bot"
REJECT_USER = "user"
REJECT_UNRESOLVED = "unresolved"
REJECT_FOREIGN_CITY = "foreign_city"
REJECT_CITY_UNKNOWN = "city_unknown"
REJECT_JOIN_REFUSED = "join_refused"
# Заявка на вступление ушла к модератору. Не «плохой чат», а потраченное
# действие: слот считается использованным (см. `ChatJoiner`).
REJECT_JOIN_REQUEST_SENT = "join_request_sent"
# Группа принимает только по заявке. Вступить молча нельзя, а заявка — это
# исходящий запрос, который стоит суточного слота ни за что.
REJECT_REQUEST_NEEDED = "request_needed"
# Мы в этом чате уже состоим, а в реестре его нет. Вступать некуда;
# заводить запись — работа не разведки, поэтому причина видна в журнале.
REJECT_ALREADY_MEMBER = "already_member"


@dataclass(frozen=True, slots=True)
class ChatCandidate:
    """Найденная ссылка на чат до всякой проверки.

    `key` — нормализованная форма (`@username` в нижнем регистре либо `+hash`),
    единственное, по чему кандидат сверяется с реестром, очередью и журналом
    отклонённых. Хэш приглашения регистрозависим, поэтому в нижний регистр не
    приводится: `+AbC` и `+abc` — разные ссылки.

    `found_in` — где встретили. Нужно не для красоты: кандидат из плотной
    барахолки и кандидат из случайного чата стоят разного, и разбирать
    накопившуюся очередь без этого поля придётся вслепую.

    `priority` — порядок разбора очереди, меньше значит раньше. Стартовый набор
    из docs/chats-nha-trang.md сидируется волнами 10/20/30, находки разведки
    идут со 100. При трёх вступлениях в сутки очередь живёт неделями, и порядок
    решает, что появится в выдаче в первую очередь.
    """

    key: str
    username: str = ""
    invite_hash: str = ""
    found_in: str = ""
    priority: int = DISCOVERED_PRIORITY


@dataclass(frozen=True, slots=True)
class ResolvedChat:
    """Что Telegram рассказал о чате, не вступая в него.

    Ради этой записи разведка и устроена так: `resolve_username` отдаёт тип,
    название и описание без единого исходящего действия. Решение о вступлении
    принимается здесь, а не после — выйти из чата нельзя вовсе (в закрытом
    списке CLAUDE.md такого действия нет), так что ошибка вступления
    невозвратна.

    По приглашению то же самое отдаёт `messages.checkChatInvite` — тоже чтение.
    Двух записей не нужно: отбор смотрит на одни и те же признаки, откуда бы
    они ни пришли, и `screen()` поэтому один на оба пути.

    `tg_id` у непройденного приглашения **нулевой**: до вступления Telegram id
    закрытого чата не отдаёт. Значит сверять кандидата с реестром по id можно
    только когда id есть.
    """

    tg_id: int
    username: str
    title: str
    about: str = ""
    is_group: bool = False
    is_bot: bool = False
    is_user: bool = False
    participants: int = 0
    # Только для приглашений: вступление идёт через заявку к модератору.
    request_needed: bool = False
    # Только для приглашений: мы в этом чате уже состоим.
    already_member: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredChat:
    """Строка, которую разведка кладёт в реестр `chats`."""

    tg_id: int
    username: str
    title: str
    city: str
    search_rank: int = DISCOVERED_RANK


@dataclass(frozen=True, slots=True)
class JoinState:
    """Снимок лимитов вступления. Считается по журналу в БД, не по памяти.

    Нужен для объяснения отказа в логе, а не для решения: решение принимает
    `claim_slot`, атомарно. Проверить и потом действовать — это гонка, а два
    воркера, одновременно прошедшие проверку, дадут четвёртое вступление.
    """

    joins_in_window: int
    next_allowed_at: datetime | None = None
    blocked_until: datetime | None = None


class ChatRegistry(Protocol):
    """Реестр чатов на запись: куда попадает находка.

    Реализуется слоем `db` поверх таблицы `chats` — тонкой обёрткой над
    `ChatRepository` (`get_by_tg_id` → `has_chat`, `add` → `add`). Читает
    реестр адаптер `telegram_groups` через свой протокол; здесь только то, что
    нужно разведке, и ни методом больше.
    """

    async def has_chat(self, *, tg_id: int | None = None, username: str = "") -> bool: ...

    async def count(self) -> int: ...

    async def add(self, chat: DiscoveredChat) -> None: ...


class CandidateQueue(Protocol):
    """Очередь отобранных кандидатов между разведкой и вступлением.

    Очередь в БД, а не список в процессе: между отбором и вступлением проходят
    часы (пауза в час на каждое), и перезапуск не должен стирать отбор.

    `reserve` обязан быть атомарным — `FOR UPDATE SKIP LOCKED` по строке с
    наименьшим `priority`, — и обязан помечать кандидата занятым. Иначе два
    воркера возьмут одного и того же, и один из них потратит суточный лимит на
    чат, в котором уже состоит. Взятого кандидата дальше ждёт ровно одно из
    трёх: `drop` (вступили), `release` (вернуть в очередь), `reject` в журнале
    отклонённых. Потерять его нельзя — разбирать очередь больше некому.
    """

    async def push(self, candidate: ChatCandidate) -> None: ...

    async def reserve(self) -> ChatCandidate | None: ...

    async def release(self, key: str) -> None: ...

    async def drop(self, key: str) -> None: ...

    async def is_queued(self, key: str) -> bool: ...


class RejectedLog(Protocol):
    """Журнал отклонённых: кандидат, который отбраковали, не разбирается снова.

    Без него каждая ссылка на дананговскую барахолку стоила бы одного
    `resolve_username` при каждой встрече, а встречается она постоянно.
    """

    async def is_rejected(self, key: str) -> bool: ...

    async def reject(self, key: str, reason: str) -> None: ...


class JoinLedger(Protocol):
    """Журнал вступлений — единственный источник правды по лимитам.

    Значений по умолчанию у него нет осознанно: реализации в памяти не
    существует и не будет. Заглушка, которую забыли заменить, выглядела бы
    рабочей и молча разрешала бы три вступления после каждого перезапуска —
    отказ, который замечают по бану аккаунта, а не по красному тесту.

    Ключевой метод — `claim_slot`. Он **одной транзакцией** проверяет все три
    временны́х ворота (стоп после флуда, три вступления в скользящие сутки,
    пауза после прошлого) и тут же занимает слот записью события. Разделить
    его на «спросить» и «записать» нельзя: между этими двумя шагами второй
    воркер успевает пройти те же ворота, и лимит превращается в пожелание.

    Занятый слот не возвращается сам. Если после `claim_slot` связь оборвалась
    и неизвестно, прошло вступление или нет, слот остаётся потраченным: лучше
    недосчитать одно вступление, чем сделать четвёртое.
    """

    async def state(self, now: datetime) -> JoinState: ...

    async def claim_slot(self, now: datetime, *, next_allowed_at: datetime) -> int | None: ...

    async def confirm_join(self, *, event_id: int, tg_id: int, username: str) -> None: ...

    async def release_slot(self, *, event_id: int) -> None: ...

    async def record_flood(self, *, event_id: int, blocked_until: datetime) -> None: ...

    async def record_mute_failure(self, *, tg_id: int, error: str) -> None: ...

    async def mark_muted(self, *, tg_id: int) -> None: ...

    async def pending_mutes(self) -> Sequence[int]: ...


def why(exc: Exception) -> str:
    """Ошибка в лог одной строкой: тип плюс текст.

    Живёт здесь, а не в каждом модуле разведки: формат строки — общее знание,
    и разъехавшись, он сделает логи трёх модулей несопоставимыми.
    """
    return f"{type(exc).__name__}: {exc}"


class EntityLike(Protocol):
    """Сущность сообщения в той части, которая нас касается.

    Интересен только `url`: у `MessageEntityTextUrl` адрес спрятан за подписью
    и в тексте сообщения его нет вообще. Это самая частая форма перекрёстной
    ссылки — «наш второй чат» с адресом под словом.
    """

    @property
    def url(self) -> str | None: ...


class MessageLike(Protocol):
    """Сообщение в объёме, который читает разведка."""

    @property
    def id(self) -> int: ...

    @property
    def message(self) -> str | None: ...

    @property
    def entities(self) -> Sequence[EntityLike] | None: ...


class TelegramJoiner(Protocol):
    """Поверхность Telegram для разведки: чтение плюс ровно два действия.

    Читающих методов три (`resolve_username`, `check_invite`,
    `search_contacts`), действий ровно два: `join_public` / `join_invite` и
    `set_muted` — закрытый список исключений из CLAUDE.md. Чтение в этот список
    не входит и входить не может: оно никому не адресовано и в чате не видно,
    поэтому под `PEER_FLOOD` не подпадает. Ни `send_message`, ни `send_reaction`, ни
    `send_read_acknowledge`, ни `forward_messages` сюда не попадают: аккаунт
    ловит `PEER_FLOOD` за исходящие (spec-v2, 6.1), а сервис на одном аккаунте
    это означает простой.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def resolve_username(self, username: str) -> ResolvedChat | None: ...

    async def check_invite(self, invite_hash: str) -> ResolvedChat | None: ...

    async def search_contacts(self, query: str, limit: int) -> Sequence[ResolvedChat]: ...

    async def join_public(self, username: str) -> int: ...

    async def join_invite(self, invite_hash: str) -> int: ...

    async def set_muted(self, tg_id: int) -> None: ...
