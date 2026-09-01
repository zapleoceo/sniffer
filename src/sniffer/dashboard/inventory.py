"""Страница «База»: что уже накоплено и поедет ли по этому поиск.

Отдельно от `views.py` не по длине файла, а по предмету. `views.py` отвечает
на вопрос «что делали клиенты»; здесь вопрос обратный и внутренний — «есть ли
чему отвечать». До первой карточки в выдаче это единственный способ увидеть,
что разведка вступает, ингест читает, а сырьё копится, — раньше всё это было
видно только через `psql` на сервере.

Разметки своей здесь нет: ячейки собираются теми же `cell()` / `num()`, что и
везде, потому что тексты объявлений приезжают из чужих чатов.
"""

from __future__ import annotations

from sniffer.dashboard import data
from sniffer.dashboard.html import Cell, cards, cell, moment, num, page, table
from sniffer.domain.records import CandidateState, JoinEvent, RawMessage

# Сколько текста объявления показываем в таблице. Полный текст — это стена на
# сорок строк; здесь достаточно узнать сообщение в лицо.
TEXT_PREVIEW = 160

# Исход вступления → как назвать его человеку. Строки те же, что пишет
# `JoinLedgerRepository`; неизвестный исход показываем как есть, а не прячем.
JOIN_KIND = {
    "claimed": "слот занят, исхода нет",
    "joined": "вступили",
    "flood": "флуд-стоп",
}


def inventory_page(view: data.Inventory) -> str:
    body = (
        "<main>"
        + _filling(view)
        + _chats(view)
        + _queue(view)
        + _joins(view)
        + _rejects(view)
        + _raw(view)
        + "</main>"
    )
    return page("SnifferBot — база", body)


def _filling(view: data.Inventory) -> str:
    """Ответ на главный вопрос страницы одной строкой карточек."""
    stats = view.stats
    limits = view.limits
    joins = "—" if limits is None else f"{limits.joins_in_window}/{view.join_ceiling}"
    return (
        "<section><h2>Наполнение</h2>"
        + cards(
            [
                ("чатов в реестре", f"{stats.get('chats_active', 0)}/{stats.get('chats', 0)}"),
                ("сообщений собрано", stats.get("raw_messages", 0)),
                ("карточек", f"{stats.get('listings_fresh', 0)}/{stats.get('listings', 0)}"),
                ("кандидатов в очереди", view.candidate_counts.get("queued", 0)),
                ("отклонено", len(view.rejects)),
                ("вступлений за сутки", joins),
                ("следующее можно", moment(None if limits is None else limits.next_allowed_at)),
                ("стоп до", moment(None if limits is None else limits.blocked_until)),
            ]
        )
        + _verdict(view)
        + "</section>"
    )


def _verdict(view: data.Inventory) -> str:
    """Прямым текстом: поедет ли поиск по группам прямо сейчас.

    Карточки показывают числа, но «12 сообщений» само по себе не говорит, много
    это или ничего. Вывод из чисел делаем здесь и один раз, чтобы владелец не
    делал его в уме каждый раз заново.
    """
    if not view.stats.get("chats"):
        return (
            "<p class='bad'>Реестр пуст: искать по группам не по чему. "
            "Смотри очередь вступлений ниже.</p>"
        )
    if not view.stats.get("raw_messages"):
        return (
            "<p class='bad'>Чаты есть, сырья нет: ингест ещё не прошёл "
            "или чтение падает — смотри «сколько собрано» по чатам.</p>"
        )
    return (
        f"<p class='good'>Поиск по группам работает: {view.stats.get('chats_active', 0)} "
        f"активных чатов, {view.stats.get('raw_messages', 0)} сообщений в сырье.</p>"
    )


def _chats(view: data.Inventory) -> str:
    rows = [
        [
            cell(row.chat.username or row.chat.tg_id),
            cell(row.chat.title),
            cell(row.chat.city),
            cell("да" if row.chat.is_active else "нет", css="" if row.chat.is_active else "bad"),
            num(row.chat.search_rank),
            num(row.harvested),
            num(row.chat.last_msg_id),
            cell(moment(row.chat.last_synced_at), css="" if row.chat.last_synced_at else "mute"),
        ]
        for row in view.chats
    ]
    return (
        "<section><h2>Чаты реестра</h2>"
        + table(
            ["чат", "название", "город", "активен", "ранг", "собрано", "курсор", "прочитан"],
            rows,
        )
        + "</section>"
    )


def _queue(view: data.Inventory) -> str:
    return (
        "<section><h2>Очередь вступлений</h2>"
        + table(
            ["кандидат", "откуда", "приоритет", "статус", "попыток", "найден"],
            [_candidate_row(candidate) for candidate in view.candidates],
        )
        + "</section>"
    )


def _candidate_row(candidate: CandidateState) -> list[Cell]:
    # Попытки красим: именно накопленные попытки означают, что очередь стоит.
    # Первый по приоритету с ненулевым счётчиком блокирует всех за собой.
    return [
        cell(candidate.key),
        cell(candidate.found_in or "владелец"),
        num(candidate.priority),
        cell(candidate.status),
        cell(candidate.attempts, css="bad" if candidate.attempts else ""),
        cell(moment(candidate.found_at)),
    ]


def _joins(view: data.Inventory) -> str:
    return (
        "<section><h2>Журнал вступлений</h2>"
        + table(
            ["когда", "исход", "чат", "заглушен", "ошибка заглушки", "пауза до", "стоп до"],
            [_join_row(event) for event in view.joins],
        )
        + "</section>"
    )


def _join_row(event: JoinEvent) -> list[Cell]:
    joined = event.kind == "joined"
    return [
        cell(moment(event.happened_at)),
        cell(JOIN_KIND.get(event.kind, event.kind), css="good" if joined else "bad"),
        cell(event.username or event.tg_id or "—"),
        # Незаглушенный чат виден владельцу уведомлениями, а не логом, поэтому
        # он здесь красный, а не просто «нет».
        cell("да" if event.muted else "нет", css="" if event.muted or not joined else "bad"),
        cell(event.mute_error or "—", css="bad" if event.mute_error else "mute"),
        cell(moment(event.next_allowed_at)),
        cell(moment(event.blocked_until), css="bad" if event.blocked_until else ""),
    ]


def _rejects(view: data.Inventory) -> str:
    rows = [
        [cell(item.key), cell(item.reason), cell(moment(item.rejected_at))] for item in view.rejects
    ]
    return (
        "<section><h2>Отклонённые</h2>"
        + table(["кандидат", "причина", "когда"], rows)
        + "</section>"
    )


def _raw(view: data.Inventory) -> str:
    return (
        "<section><h2>Последнее сырьё</h2>"
        + table(
            ["опубликовано", "чат", "медиа", "текст"],
            [_raw_row(message) for message in view.raw],
        )
        + "</section>"
    )


def _raw_row(message: RawMessage) -> list[Cell]:
    text = message.text[:TEXT_PREVIEW]
    if len(message.text) > TEXT_PREVIEW:
        text += "…"
    return [
        cell(moment(message.posted_at)),
        cell(message.chat_tg_id),
        cell("да" if message.has_media else "нет"),
        # Текст из чужого чата. Экранируется, как и всё остальное, самим `cell`.
        cell(text, css="msg"),
    ]
