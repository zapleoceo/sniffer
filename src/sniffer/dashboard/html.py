"""Разметка страниц. Серверный рендеринг, без сборки фронтенда.

Страница внутренняя и для одного человека, поэтому шаблонизатора и бандлера
здесь нет: их пришлось бы обслуживать, а выигрыша ноль.

**Единственное правило этого модуля: наружу ничего не уходит без `esc()`.**
Тексты объявлений и сообщения клиентов приезжают из чужих чатов и содержат что
угодно, включая `<script>` и кавычки в атрибутах. Поэтому `esc()` экранирует и
кавычки тоже, а собственная разметка собирается только здесь.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

STYLE = """
:root { color-scheme: dark; --bg:#0f1115; --panel:#1a1d24; --line:#2b303a;
        --text:#e4e6eb; --mute:#9aa0a8; --accent:#4dabf7; --bad:#ff9d9d;
        --good:#8ce99a; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5
       -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color:var(--accent); }
header { padding:20px 24px; border-bottom:1px solid var(--line);
         display:flex; gap:20px; align-items:baseline; flex-wrap:wrap; }
header h1 { font-size:19px; margin:0; }
nav a { margin-right:14px; }
main { padding:24px; max-width:1200px; }
section { margin-bottom:34px; }
h2 { font-size:16px; margin:0 0 12px; color:var(--mute);
     text-transform:uppercase; letter-spacing:.06em; }
.cards { display:flex; flex-wrap:wrap; gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
        padding:14px 18px; min-width:150px; }
.card b { display:block; font-size:22px; font-weight:600; }
.card span { color:var(--mute); font-size:13px; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; background:var(--panel);
        border:1px solid var(--line); border-radius:12px; }
th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line);
         vertical-align:top; font-size:14px; }
th { color:var(--mute); font-weight:600; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.msg { white-space:pre-wrap; word-break:break-word; max-width:720px; }
.in { border-left:3px solid var(--accent); padding-left:10px; }
.out { border-left:3px solid var(--mute); padding-left:10px; }
.bad { color:var(--bad); }
.good { color:var(--good); }
.mute { color:var(--mute); }
.box { background:var(--panel); border:1px solid var(--line); border-radius:12px;
       padding:20px; max-width:460px; }
input { width:100%; padding:10px 12px; margin:6px 0 14px; border-radius:8px;
        border:1px solid var(--line); background:var(--bg); color:var(--text);
        font-size:15px; }
button { padding:9px 18px; border:none; border-radius:8px; background:var(--accent);
         color:#08111c; font-weight:600; font-size:15px; cursor:pointer; }
.center { min-height:100vh; display:flex; align-items:center; justify-content:center; }
"""


def esc(value: object) -> str:
    """Всё, что приехало не от нас, проходит здесь. Кавычки тоже."""
    return escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, *, nav: bool = True) -> str:
    header = (
        f"<header><h1>{esc(title)}</h1><nav>"
        '<a href="/">Обзор</a><a href="/session">Сессия юзербота</a>'
        "</nav></header>"
        if nav
        else ""
    )
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        # Страница показывает переписку клиентов. Пусть её не тянет ни поиск, ни
        # реферер на чужой домен.
        '<meta name="robots" content="noindex, nofollow">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>"
        f"{header}{body}</body></html>"
    )


def login_page(bot_username: str) -> str:
    """Только виджет и ничего больше: неавторизованный не видит данных."""
    body = (
        '<div class="center"><div class="box" style="text-align:center">'
        "<h1 style='font-size:22px;margin:0 0 6px'>SnifferBot</h1>"
        "<p class='mute'>Интерфейс владельца. Вход через Telegram.</p>"
        '<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{esc(bot_username)}" data-size="large" '
        'data-radius="10" data-auth-url="/auth/telegram" '
        'data-request-access="write"></script>'
        "</div></div>"
    )
    return page("SnifferBot — вход", body, nav=False)


def error_page(status: int, message: str) -> str:
    body = (
        '<div class="center"><div class="box" style="text-align:center">'
        f"<h1 style='font-size:22px' class='bad'>{esc(status)}</h1>"
        f"<p>{esc(message)}</p><p><a href='/'>← на главную</a></p></div></div>"
    )
    return page(f"SnifferBot — {status}", body, nav=False)


def cards(items: list[tuple[str, object]]) -> str:
    return (
        '<div class="cards">'
        + "".join(
            f"<div class='card'><b>{esc(value)}</b><span>{esc(label)}</span></div>"
            for label, value in items
        )
        + "</div>"
    )


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Заголовки экранируются, ячейки — НЕТ: их готовит вызывающий.

    Так сделано затем, чтобы в ячейке могла быть ссылка. Ответственность на
    вызывающем: любой текст из базы он обязан прогнать через `esc()` сам.
    """
    if not rows:
        return "<p class='mute'>пусто</p>"
    head = "".join(f"<th>{esc(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table><tr>{head}</tr>{body}</table></div>"


def cell(value: object, *, css: str = "") -> str:
    """Ячейка с экранированным содержимым."""
    klass = f" class='{esc(css)}'" if css else ""
    return f"<td{klass}>{esc(value)}</td>"


def num(value: object) -> str:
    return f"<td class='num'>{esc(value)}</td>"


def link(href: str, text: object) -> str:
    return f"<td><a href='{esc(href)}'>{esc(text)}</a></td>"


def moment(value: datetime | None) -> str:
    """Время в UTC. Одна зона на всю страницу честнее, чем угаданная локальная."""
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def money(value: Decimal | None) -> str:
    """Стоимость в долларах. Шесть знаков — как в колонке NUMERIC(12,6)."""
    return "—" if value is None else f"${value:.6f}"


def millis(value: int | None) -> str:
    return "—" if value is None else f"{value / 1000:.1f} с"
