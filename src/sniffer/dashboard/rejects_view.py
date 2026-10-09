"""Отклонённые кандидаты на странице «База»: счёт, разбивка и адресный повтор.

Отдельный модуль по предмету: `inventory.py` отвечает за «что накоплено», а здесь
вопрос другой — «почему не взяли и вернётся ли». Единственные элементы управления —
кнопка «Повторить» у ВРЕМЕННОГО отказа, по одному ключу на форму; «выбрать все» и
массового маршрута нет (модель угроз — docs/dashboard.md). Решение, включена ли
кнопка, приходит готовым из `domain/reject_retry.py`: то же правило сервер применяет
под замком при POST, так что страница не может разойтись с ним.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

from sniffer.dashboard import data
from sniffer.dashboard.html import Cell, cell, esc, moment, num, table
from sniffer.domain import reject_reasons, reject_retry
from sniffer.domain.records import RejectedCandidate
from sniffer.domain.reject_reasons import RejectClass
from sniffer.domain.reject_retry import RetryOffer, RetryRecord
from sniffer.sources.telegram_discover_reference import MAX_JOINS_PER_DAY

# Цвет класса: постоянный — спокойный (так и задумано), остальное требует взгляда.
CLASS_CSS = {
    RejectClass.PERMANENT: "mute",
    RejectClass.TEMPORARY: "bad",
    RejectClass.MEMBERSHIP: "mute",
    RejectClass.PENDING: "bad",
    RejectClass.UNKNOWN: "bad",
}

RETRY_PATH = "/rejects/{key}/retry"

# Исход попытки → слова. Неизвестный показываем как есть.
OUTCOME = {
    "left_queue": "ушёл из очереди (вступили или сняли)",
    "rejected_again": "отклонён снова",
}


def total(view: data.Inventory) -> int:
    """Всего отклонённых — сумма по причинам, а не длина показанного хвоста."""
    return sum(view.reject_counts.values())


def card_label(view: data.Inventory) -> str:
    """Подпись карточки: сколько из общего числа отклонённых — временные."""
    by_class = reject_reasons.totals_by_class(view.reject_counts)
    return f"отклонено (временных: {by_class[RejectClass.TEMPORARY]})"


def section(view: data.Inventory, *, csrf: str) -> str:
    shown = len(view.rejects)
    return (
        f"<section><h2>Отклонённые: последние {shown} из {total(view)}</h2>"
        + _breakdown(view)
        + table(
            ["кандидат", "причина", "класс", "когда", "повтор"],
            [_row(item) for item in view.rejects],
        )
        + "</section>"
        + _retry_section(view, csrf)
    )


def _breakdown(view: data.Inventory) -> str:
    ordered = sorted(view.reject_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return table(
        ["причина", "класс", "всего"],
        [[_reason(reason), _class(reason), num(count)] for reason, count in ordered],
    )


def _row(item: RejectedCandidate) -> list[Cell]:
    refusal = reject_retry.not_retryable(item.reason)
    return [
        cell(item.key),
        _reason(item.reason),
        _class(item.reason),
        cell(moment(item.rejected_at)),
        cell(refusal.message if refusal else "см. блок «Повтор временных отказов»", css="mute"),
    ]


def _reason(reason: str) -> Cell:
    return cell(reject_reasons.label(reason))


def _class(reason: str) -> Cell:
    kind = reject_reasons.classify(reason)
    return cell(reject_reasons.CLASS_LABEL[kind], css=CLASS_CSS[kind])


# ── повтор временных отказов ────────────────────────────────────────────────


def _retry_section(view: data.Inventory, csrf: str) -> str:
    rows = [
        _retry_row(item, view.retry_offers.get(item.key), csrf) for item in view.temporary_rejects
    ]
    hours = int(reject_retry.COOLDOWN.total_seconds() // 3600)
    return (
        "<section><h2>Повтор временных отказов</h2>"
        f"<p class='mute'>{esc(cap_note(view))}</p>"
        f"<p class='mute'>Запросов повтора за скользящие сутки: {view.retries_today} из "
        f"{reject_retry.MAX_RETRIES_PER_DAY}. Попыток на ключ — не больше "
        f"{reject_retry.MAX_RETRIES_PER_KEY}, между попытками — {hours} ч. Повтор ничего "
        "не отправляет в Telegram: он только возвращает ключ в очередь, а вступает тот "
        f"же joiner по своим лимитам (не больше {MAX_JOINS_PER_DAY} в сутки).</p>"
        + table(["кандидат", "причина", "когда", "попыток", "действие"], rows)
        + "<h3>Журнал попыток</h3>"
        + _history(view.retries)
        + "</section>"
    )


def cap_note(view: data.Inventory) -> str:
    """Предупреждение о потолке чатов — из реального счёта и потолка, без зашитых чисел."""
    if view.tracked_chats >= view.chat_cap:
        return (
            f"Сейчас {view.tracked_chats} из {view.chat_cap} чатов: лимит заполнен. "
            "Повтор поставит ключ в очередь, но вступлений не будет, пока лимит заполнен."
        )
    return (
        f"Повторенный ключ встанет в очередь; сейчас {view.tracked_chats} из {view.chat_cap} чатов."
    )


def _retry_row(item: RejectedCandidate, offer: RetryOffer | None, csrf: str) -> list[Cell]:
    return [
        cell(item.key),
        _reason(item.reason),
        cell(moment(item.rejected_at)),
        cell(f"{offer.attempts_used if offer else 0} из {reject_retry.MAX_RETRIES_PER_KEY}"),
        _action(item.key, offer, csrf),
    ]


def _action(key: str, offer: RetryOffer | None, csrf: str) -> Cell:
    if offer is None:
        return Cell(
            "<td><button disabled>Повторить</button> <span class='mute'>нет данных</span></td>"
        )
    if not offer.decision.allowed:
        until = offer.decision.available_at
        when = f" — доступно с {moment(until)} UTC" if until else ""
        return Cell(
            "<td><button disabled>Повторить</button> "
            f"<span class='mute'>{esc(offer.decision.message)}{esc(when)}</span></td>"
        )
    action = RETRY_PATH.format(key=quote(key, safe=""))
    # Токен формы свежий на каждую отрисовку: повторная отправка ЭТОЙ формы (двойной
    # клик, перезагрузка после POST) не заводит вторую попытку.
    token = secrets.token_urlsafe(12)
    return Cell(
        f"<td><form method='post' action='{esc(action)}'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        f"<input type='hidden' name='request_id' value='{esc(token)}'>"
        "<button type='submit'>Повторить</button></form></td>"
    )


def _history(records: list[RetryRecord]) -> str:
    return table(
        ["запрошено", "кандидат", "был отказ", "состояние", "следующая попытка с"],
        [_history_row(record) for record in records],
    )


def _history_row(record: RetryRecord) -> list[Cell]:
    was = f"{reject_reasons.label(record.reject_reason)} ({moment(record.reject_rejected_at)})"
    state = (
        "в очереди"
        if record.status == "active"
        else OUTCOME.get(record.outcome or "", record.outcome or "—")
    )
    return [
        cell(moment(record.requested_at)),
        cell(record.reject_key),
        cell(was),
        cell(state),
        cell(moment(record.next_retry_at)),
    ]
