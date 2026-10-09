"""Причины отказа кандидату в чат: как их называть человеку и что они значат.

Журнал `chat_rejects` хранит голую строку-причину. Для владельца важнее другое:
«отклонён навсегда» это или «упал по сбою и мог бы пройти». Раньше этот вывод
приходилось делать из названия кода в уме. Классификация живёт здесь, в одном
месте; новая причина без записи в словаре не проходит молча, а показывается как
«неизвестно».

Неизвестная причина НЕ считается постоянной: «постоянный» — это утверждение,
что кандидат больше не вернётся, и его нельзя делать про код, которого не знаем.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RejectClass(StrEnum):
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    # Не отказ по существу, а состояние: мы уже внутри, либо ждём модератора или
    # действия человека. Слепой повтор здесь бессмыслен — показывается отдельно.
    MEMBERSHIP = "membership"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RejectInfo:
    kind: RejectClass
    label: str


CLASS_LABEL: dict[RejectClass, str] = {
    RejectClass.TEMPORARY: "временный",
    RejectClass.PERMANENT: "постоянный",
    RejectClass.MEMBERSHIP: "уже участник",
    RejectClass.PENDING: "ожидание / нужно действие",
    RejectClass.UNKNOWN: "неизвестно",
}

_T = RejectClass.TEMPORARY
_P = RejectClass.PERMANENT
_M = RejectClass.MEMBERSHIP
_W = RejectClass.PENDING
_U = RejectClass.UNKNOWN

# Ключи — те же строки, что пишет разведка (`REJECT_*` в
# sources/telegram_discover_reference.py). Домен не импортирует `sources`
# (обратное ребро слоёв), поэтому строки продублированы, а их совпадение с
# источником сторожит тест.
REASONS: dict[str, RejectInfo] = {
    # Вступление много раз подряд кончалось неизвестным исходом — сбой, а не
    # суждение о чате.
    "too_many_attempts": RejectInfo(_T, "слишком много неудачных попыток"),
    # `unresolved` сам по себе не доказывает сетевой сбой: до 10.2026 он
    # писался и на «такого чата нет», и на сеть/таймаут/FloodWait. Старые записи
    # неоднозначны — честно «неизвестно», а не «временный».
    "unresolved": RejectInfo(_U, "чат не найден или сбой при проверке"),
    # Город по чату не определился — это незнание, а не вердикт.
    "city_unknown": RejectInfo(_U, "город чата не виден"),
    # Состояния, а не отказы по существу.
    "already_member": RejectInfo(_M, "мы уже в этом чате"),
    "already_inside": RejectInfo(_M, "оказалось, мы уже внутри"),
    "join_request_sent": RejectInfo(_W, "заявка ушла модератору — ждём"),
    "request_needed": RejectInfo(_W, "вход только по заявке — нужно действие"),
    # Суждение о самом чате или известный исход запроса.
    "user": RejectInfo(_P, "это человек, а не группа"),
    "channel": RejectInfo(_P, "канал, а не группа"),
    "bot": RejectInfo(_P, "бот"),
    "foreign_city": RejectInfo(_P, "чат другого города"),
    "join_refused": RejectInfo(_P, "Telegram отказал во вступлении"),
}


def classify(reason: str) -> RejectClass:
    info = REASONS.get(reason)
    return info.kind if info else RejectClass.UNKNOWN


def label(reason: str) -> str:
    """Понятная причина; для неизвестной — сам код, чтобы его можно было найти."""
    info = REASONS.get(reason)
    return info.label if info else reason


def class_label(reason: str) -> str:
    return CLASS_LABEL[classify(reason)]


def totals_by_class(counts: dict[str, int]) -> dict[RejectClass, int]:
    """Сколько отклонённых в каждом классе — по разбивке причин."""
    out = dict.fromkeys(RejectClass, 0)
    for reason, total in counts.items():
        out[classify(reason)] += total
    return out
