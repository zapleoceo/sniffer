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

from collections.abc import Sequence
from datetime import datetime
from html import escape

from sniffer.sources.base import RawItem
from sniffer.verifier.liveness import Liveness, as_utc, assess

# Пять — не техническое ограничение, а продуктовое: длинная выдача не
# просматривается, а пролистывается (architecture.md, раздел 11).
MAX_CARDS = 5
TITLE_LIMIT = 90


def render_cards(
    items: Sequence[RawItem],
    *,
    now: datetime | None = None,
    limit: int = MAX_CARDS,
) -> str:
    return "\n\n".join(render_card(item, now=now) for item in items[:limit])


def render_card(item: RawItem, *, now: datetime | None = None) -> str:
    verdict = assess(item.posted_at, now=now)
    facts = " · ".join(part for part in (_price(item), _posted(item)) if part)
    lines = [f"<b>{escape(_title(item))}</b>", facts]

    if verdict.status is Liveness.STALE and verdict.age_days is not None:
        lines.append(f"объявлению {verdict.age_days} {_days(verdict.age_days)}, могло быть продано")
    elif verdict.status is Liveness.UNKNOWN:
        lines.append("дата публикации неизвестна, свежесть не проверить")

    lines.append(f'<a href="{escape(item.url, quote=True)}">открыть оригинал</a> · {item.source}')
    return "\n".join(lines)


def _days(count: int) -> str:
    """«21 день», «22 дня», «25 дней»: бот, который путает склонения, выглядит сломанным."""
    if 11 <= count % 100 <= 14:
        return "дней"
    last = count % 10
    if last == 1:
        return "день"
    return "дня" if last in (2, 3, 4) else "дней"


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
        return escape(item.price_raw.strip())
    if item.price_vnd:
        return f"{item.price_vnd:,} ₫".replace(",", " ")
    return "цена не указана"


def _posted(item: RawItem) -> str:
    if item.posted_at is None:
        return ""
    return as_utc(item.posted_at).strftime("%d.%m.%Y")
