"""Отклонённые кандидаты на странице «База»: настоящий счёт и разбивка.

Отдельный модуль по предмету: `inventory.py` отвечает за «что накоплено», а здесь
вопрос другой — «почему не взяли и вернётся ли». Страница только читает: кнопок
повтора и POST-маршрутов тут нет и не будет (модель угроз — docs/dashboard.md).
"""

from __future__ import annotations

from sniffer.dashboard import data
from sniffer.dashboard.html import Cell, cell, moment, num, table
from sniffer.domain import reject_reasons
from sniffer.domain.records import RejectedCandidate
from sniffer.domain.reject_reasons import RejectClass

# Цвет класса: постоянный — спокойный (так и задумано), остальное требует взгляда.
CLASS_CSS = {
    RejectClass.PERMANENT: "mute",
    RejectClass.TEMPORARY: "bad",
    RejectClass.UNKNOWN: "bad",
}


def total(view: data.Inventory) -> int:
    """Всего отклонённых — сумма по причинам, а не длина показанного хвоста."""
    return sum(view.reject_counts.values())


def card_label(view: data.Inventory) -> str:
    """Подпись карточки: сколько из общего числа отклонённых — временные."""
    by_class = reject_reasons.totals_by_class(view.reject_counts)
    return f"отклонено (временных: {by_class[RejectClass.TEMPORARY]})"


def section(view: data.Inventory) -> str:
    shown = len(view.rejects)
    return (
        f"<section><h2>Отклонённые: последние {shown} из {total(view)}</h2>"
        + _breakdown(view)
        + table(
            ["кандидат", "причина", "класс", "когда"],
            [_row(item) for item in view.rejects],
        )
        + "</section>"
    )


def _breakdown(view: data.Inventory) -> str:
    ordered = sorted(view.reject_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return table(
        ["причина", "класс", "всего"],
        [[_reason(reason), _class(reason), num(count)] for reason, count in ordered],
    )


def _row(item: RejectedCandidate) -> list[Cell]:
    return [
        cell(item.key),
        _reason(item.reason),
        _class(item.reason),
        cell(moment(item.rejected_at)),
    ]


def _reason(reason: str) -> Cell:
    return cell(reject_reasons.label(reason))


def _class(reason: str) -> Cell:
    kind = reject_reasons.classify(reason)
    return cell(reject_reasons.CLASS_LABEL[kind], css=CLASS_CSS[kind])
