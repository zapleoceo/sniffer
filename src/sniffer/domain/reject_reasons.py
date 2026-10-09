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
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RejectInfo:
    kind: RejectClass
    label: str


CLASS_LABEL: dict[RejectClass, str] = {
    RejectClass.TEMPORARY: "временный",
    RejectClass.PERMANENT: "постоянный",
    RejectClass.UNKNOWN: "неизвестно",
}

_T = RejectClass.TEMPORARY
_P = RejectClass.PERMANENT

# Ключи — те же строки, что пишет разведка (`REJECT_*` в
# sources/telegram_discover_reference.py). Домен не импортирует `sources`
# (обратное ребро слоёв), поэтому строки продублированы, а их совпадение с
# источником сторожит тест.
REASONS: dict[str, RejectInfo] = {
    # Сбой, а не суждение о чате: адрес не разрешился либо вступление много раз
    # подряд кончалось неизвестным исходом.
    "unresolved": RejectInfo(_T, "не удалось определить чат"),
    "too_many_attempts": RejectInfo(_T, "слишком много неудачных попыток"),
    # Суждение о самом чате или известный исход запроса.
    "user": RejectInfo(_P, "это человек, а не группа"),
    "channel": RejectInfo(_P, "канал, а не группа"),
    "bot": RejectInfo(_P, "бот"),
    "foreign_city": RejectInfo(_P, "чат другого города"),
    "city_unknown": RejectInfo(_P, "город чата не виден"),
    "already_member": RejectInfo(_P, "мы уже в этом чате"),
    "already_inside": RejectInfo(_P, "оказалось, мы уже внутри"),
    "join_request_sent": RejectInfo(_P, "заявка ушла модератору"),
    "request_needed": RejectInfo(_P, "вход только по заявке"),
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
