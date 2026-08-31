"""Сборка страниц из данных. Ни одного обращения к базе — только разметка."""

from __future__ import annotations

from decimal import Decimal

from sniffer.dashboard import data
from sniffer.dashboard.html import cards, cell, esc, link, millis, moment, money, num, page, table
from sniffer.domain.records import DIRECTION_IN, REQUEST_FAILED, DialogMessage, SessionState

# Порядок этапов задаём явно. Не по алфавиту и не по порядку вставки: `jsonb` в
# Postgres хранит ключи в своём порядке, и после записи в базу хронология
# теряется. Незнакомые этапы (появятся с новыми ступенями воронки) идут после
# известных, чтобы таблица не молчала о них.
STAGE_ORDER = ("intake_ms", "plan_ms", "search_ms")


def overview_page(view: data.Overview) -> str:
    return page(
        "SnifferBot — обзор",
        f"<main>{_summary(view)}{_session_block(view.session)}"
        f"{_requests(view)}{_users(view)}</main>",
    )


def request_page(view: data.RequestDetail) -> str:
    request = view.row.request
    client = "—" if view.user is None else f"@{view.user.username or view.user.tg_user_id}"
    head = cards(
        [
            ("клиент", client),
            ("статус", request.status),
            ("найдено", request.result_count),
            ("время", millis(request.duration_ms)),
            ("токенов", view.row.tokens),
            ("стоимость", money(view.row.cost_usd)),
        ]
    )
    stages = table(
        ["этап", "время"],
        [[cell(name), cell(millis(value))] for name, value in _ordered_stages(request.stages)],
    )
    calls = table(
        ["время", "capability", "провайдер", "модель", "вход", "выход", "стоимость", "брокер"],
        [
            [
                cell(moment(call.created_at)),
                cell(call.capability),
                cell(call.provider or "—"),
                cell(call.model or "—"),
                num(call.tokens_in),
                num(call.tokens_out),
                num(money(call.cost_usd)),
                num(call.broker_request_id if call.broker_request_id is not None else "—"),
            ]
            for call in view.calls
        ],
    )
    error = (
        f"<p class='bad'>Ошибка: {esc(request.error)}</p>"
        if request.status == REQUEST_FAILED and request.error
        else ""
    )
    body = (
        f"<main><section><h2>Запрос №{esc(request.id)}</h2>"
        f"<p class='msg'>{esc(request.raw_query)}</p>{error}{head}</section>"
        f"<section><h2>Время по этапам</h2>{stages}</section>"
        f"<section><h2>Расходы на модель</h2>{calls}</section>"
        f"<section><h2>Переписка</h2>{_dialog(view.dialog)}</section></main>"
    )
    return page(f"SnifferBot — запрос {request.id}", body)


def user_page(view: data.UserDetail) -> str:
    user = view.user
    body = (
        f"<main><section><h2>Клиент {esc(user.username or user.tg_user_id)}</h2>"
        + cards(
            [
                ("telegram id", user.tg_user_id),
                ("язык", user.lang),
                ("заблокирован", "да" if user.is_blocked else "нет"),
                ("первый раз", moment(user.created_at)),
            ]
        )
        + f"</section><section><h2>Переписка</h2>{_dialog(view.dialog)}</section></main>"
    )
    return page(f"SnifferBot — клиент {user.tg_user_id}", body)


def session_page(states: list[SessionState], *, csrf: str, phone: str, note: str = "") -> str:
    rows = [
        [
            cell(state.phone),
            cell(
                "активна" if state.is_active else "не активна",
                css="good" if state.is_active else "bad",
            ),
            cell(moment(state.last_ok_at)),
            cell(state.last_error or "—", css="bad" if state.last_error else ""),
            cell(moment(state.last_error_at)),
        ]
        for state in states
    ]
    body = (
        "<main><section><h2>Сессия юзербота</h2>"
        + table(["номер", "состояние", "последний успех", "ошибка", "когда"], rows)
        + "</section><section><h2>Переавторизация</h2>"
        "<div class='box'>"
        "<p class='mute'>Telegram пришлёт код в приложение. Код и облачный пароль "
        "нужны один раз, они нигде не сохраняются.</p>"
        f"{note}"
        "<form method='post' action='/session/start'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        f"<label>Номер телефона</label><input name='phone' value='{esc(phone)}' required>"
        "<button type='submit'>Выслать код</button></form></div></section></main>"
    )
    return page("SnifferBot — сессия", body)


def code_form(flow_id: str, *, csrf: str, note: str = "", password: bool = False) -> str:
    field = (
        "<label>Облачный пароль (2FA)</label>"
        "<input name='password' type='password' autocomplete='off' autofocus required>"
        if password
        else "<label>Код из Telegram</label>"
        "<input name='code' inputmode='numeric' autocomplete='off' autofocus required>"
    )
    body = (
        "<main><section><h2>Переавторизация</h2><div class='box'>"
        f"{note}<form method='post' action='/session/verify'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        f"<input type='hidden' name='flow_id' value='{esc(flow_id)}'>"
        f"{field}<button type='submit'>Подтвердить</button></form>"
        "<p class='mute'><a href='/session'>← начать заново</a></p>"
        "</div></section></main>"
    )
    return page("SnifferBot — сессия", body)


def _ordered_stages(stages: dict[str, int]) -> list[tuple[str, int]]:
    def position(name: str) -> tuple[int, str]:
        return (STAGE_ORDER.index(name) if name in STAGE_ORDER else len(STAGE_ORDER), name)

    return sorted(stages.items(), key=lambda item: position(item[0]))


def _summary(view: data.Overview) -> str:
    stats, requests = view.stats, view.request_totals
    total = max(requests.get("requests", 0), 1)
    fallback_share = requests.get("fallbacks", 0) * 100 // total
    cost = view.cost_totals.get("cost_usd")
    return (
        "<section><h2>Сводка</h2>"
        + cards(
            [
                ("клиентов", stats.get("users", 0)),
                ("запросов", requests.get("requests", 0)),
                ("из них упало", requests.get("failed", 0)),
                ("фолбэков плана", f"{fallback_share}%"),
                ("среднее время", millis(requests.get("avg_duration_ms"))),
                ("чатов активно", f"{stats.get('chats_active', 0)}/{stats.get('chats', 0)}"),
                ("сырья", stats.get("raw_messages", 0)),
                (
                    "карточек живых",
                    f"{stats.get('listings_fresh', 0)}/{stats.get('listings', 0)}",
                ),
                ("вызовов модели", view.cost_totals.get("calls", 0)),
                ("потрачено", money(cost if isinstance(cost, Decimal) else None)),
            ]
        )
        + "</section>"
    )


def _session_block(state: SessionState | None) -> str:
    if state is None:
        status = "<p class='bad'>сессии нет — коллектор читать не может</p>"
    elif state.is_active:
        status = f"<p class='good'>активна, номер {esc(state.phone)}</p>"
    else:
        status = f"<p class='bad'>отвалилась: {esc(state.last_error or 'причина не записана')}</p>"
    return (
        f"<section><h2>Сессия юзербота</h2>{status}"
        "<p><a href='/session'>переавторизовать →</a></p></section>"
    )


def _requests(view: data.Overview) -> str:
    rows = []
    for row in view.requests:
        request = row.request
        rows.append(
            [
                link(f"/requests/{request.id}", request.id),
                cell(moment(request.started_at)),
                link(f"/users/{request.user_id}", request.user_id),
                cell(request.raw_query, css="msg"),
                cell(
                    request.status,
                    css="bad" if request.status == REQUEST_FAILED else "",
                ),
                num(request.result_count),
                cell("да" if request.plan_fallback else "нет"),
                num(millis(request.duration_ms)),
                num(row.tokens),
                num(money(row.cost_usd)),
            ]
        )
    return (
        "<section><h2>Запросы</h2>"
        + table(
            [
                "№",
                "начат",
                "клиент",
                "формулировка",
                "статус",
                "найдено",
                "фолбэк",
                "время",
                "токенов",
                "стоимость",
            ],
            rows,
        )
        + "</section>"
    )


def _users(view: data.Overview) -> str:
    rows = [
        [
            link(f"/users/{user.id}", user.id),
            cell(user.tg_user_id),
            cell(user.username or "—"),
            cell(user.lang),
            num(view.requests_by_user.get(user.id or 0, 0)),
            cell(moment(user.created_at)),
            cell("да" if user.is_blocked else "нет"),
        ]
        for user in view.users
    ]
    return (
        "<section><h2>Клиенты</h2>"
        + table(["№", "telegram id", "username", "язык", "запросов", "первый раз", "блок"], rows)
        + "</section>"
    )


def _dialog(messages: list[DialogMessage]) -> str:
    if not messages:
        return "<p class='mute'>переписки нет</p>"
    rows = [
        [
            cell(moment(message.created_at)),
            cell("клиент" if message.direction == DIRECTION_IN else "бот"),
            f"<td class='msg {'in' if message.direction == DIRECTION_IN else 'out'}'>"
            f"{esc(message.text)}</td>",
        ]
        for message in messages
    ]
    return table(["время", "кто", "текст"], rows)
