"""Карточка выдачи: минимум фактов и ссылка на оригинал.

Объявление не перепечатывается (architecture.md, раздел 1) — мы отдаём ссылку.
Это снимает вопрос ответственности за чужой контент и за контакты продавца, а
заодно не даёт карточке разойтись с оригиналом, когда автор его правит.

Пометка о возрасте — не украшение, а минимум verifier'а (spec-v2, 3.3):
объявления не снимают почти никогда, и главная боль ручного поиска — звонок по
лоту, проданному два месяца назад. Пока полного проверяльщика нет, честная
дата и предупреждение делают эту работу.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from html import escape, unescape

from sniffer.bot.naming import plural
from sniffer.config import get_settings
from sniffer.domain.card_facts import facts_line
from sniffer.sources.base import RawItem
from sniffer.verifier.liveness import Liveness, as_utc, assess

TITLE_LIMIT = 90
PRICE_LIMIT = 60
# Предел сообщения Telegram — 4096 знаков после разбора сущностей. Берём с запасом:
# разметка и `&amp;` в нашей строке длиннее видимого текста, то есть оценка консервативна.
MESSAGE_LIMIT = 4000
SOURCE_LIMIT = 40
_CHAT_SOURCES = ("telegram", "archive")


def render_cards(
    items: Sequence[RawItem],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> str:
    """Длина выдачи — настройка (`MAX_CARDS`), а не константа в коде.

    Пять карточек это лимит бесплатного тарифа (spec-v2, 5.1) и одновременно
    продуктовый предел: длинная выдача не просматривается, а пролистывается
    (architecture.md, раздел 11). Тарифов ещё нет, но платный будет отличаться
    от бесплатного значением настройки, а не веткой здесь.
    """
    cap = get_settings().max_cards if limit is None else limit
    return "\n\n".join(render_card(item, now=now) for item in items[:cap])


def render_card(item: RawItem, *, now: datetime | None = None) -> str:
    verdict = assess(item.posted_at, now=now)
    facts = " · ".join(part for part in (_price(item), _posted(item)) if part)
    title = _title(item)
    lines = [f"<b>{escape(title)}</b>", facts]
    attributes = item.raw.get("attributes")
    if details := facts_line(attributes if isinstance(attributes, dict) else None, title=title):
        lines.append(escape(details))

    if verdict.status is Liveness.STALE and verdict.age_days is not None:
        lines.append(f"объявлению {verdict.age_days} {_days(verdict.age_days)}, могло быть продано")
    elif verdict.status is Liveness.UNKNOWN:
        lines.append("дата публикации неизвестна, свежесть не проверить")

    link = f'<a href="{escape(item.url, quote=True)}">открыть оригинал</a>'
    lines.append(f"{link} · {escape(source_label(item))}")
    return "\n".join(line for line in lines if line)


def source_label(item: RawItem) -> str:
    """Откуда лот человеку: имя чата или «Chotot», а не служебное `archive`."""
    chat = item.raw.get("chat_title")
    if isinstance(chat, str) and chat.strip():
        return " ".join(chat.split())[:SOURCE_LIMIT]
    if item.source.startswith(_CHAT_SOURCES):
        return "Telegram"
    return item.source.capitalize()


_TAG = re.compile(r"<[^>]*>")


def visible_len(markup: str) -> int:
    """Длина так, как её считает Telegram: после разбора разметки, `&amp;` — один знак."""
    return len(unescape(_TAG.sub("", markup)))


def chunk(blocks: Sequence[str], *, head: str = "", limit: int = MESSAGE_LIMIT) -> list[str]:
    """Карточки по сообщениям: режем по границе карточки, а не по знаку.

    Лимит 4096 нельзя проверить глазами, а отказ Telegram бьёт по всему сообщению, а не по
    лишней карточке. `head` едет только в первом сообщении. Одна карточка длиннее лимита
    невозможна: заголовок, цена и факты обрезаны выше.
    """
    messages: list[str] = []
    current = head
    for block in blocks:
        joined = f"{current}\n\n{block}" if current else block
        if current and visible_len(joined) > limit:
            messages.append(current)
            joined = block
        current = joined
    if current:
        messages.append(current)
    return messages


def _days(count: int) -> str:
    """«21 день», «22 дня», «25 дней»: склонение одно на весь бот (`naming.plural`)."""
    return plural(count, ("день", "дня", "дней"))


def _title(item: RawItem) -> str:
    title = " ".join(item.title.split())
    if not title:
        # Заголовка нет — берём начало текста, но не весь текст: перепечатывать
        # объявление мы не имеем права.
        title = " ".join(item.text.split())
    if not title:
        return "без заголовка"
    return title if len(title) <= TITLE_LIMIT else f"{title[:TITLE_LIMIT].rstrip()}…"


def _price(item: RawItem) -> str:
    """Показываем то, что написал продавец: сверять распознанное число не с чем."""
    if item.price_raw.strip():
        return escape(item.price_raw.strip()[:PRICE_LIMIT])
    if item.price_vnd:
        return f"{item.price_vnd:,} ₫".replace(",", " ")
    return "цена не указана"


def _posted(item: RawItem) -> str:
    if item.posted_at is None:
        return ""
    return as_utc(item.posted_at).strftime("%d.%m.%Y")
