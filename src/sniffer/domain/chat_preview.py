"""Публичное превью чата t.me/<username>: разбор страницы и слова, по которым его читают.

Без ввода-вывода: страницу приносит `sources/chat_preview_fetch.py`, класс выводит
`search/chat_preview.py` (ему нужен словарь рынка и города). Здесь - то, что от них
не зависит: форма снимка, разбор og-тегов и регулярки рыночных / нерыночных слов.

Превью читается без юзербота и без модели, поэтому оно грубое, и грубость
учтена в правилах: по одному названию чат не отвергают, а слово, найденное только в
ссылке или в рекламном контакте («@rent_admin», «t.me/bike_shop»), доказательством не
считается - это адрес автора, а не тема чата.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

# Классы кандидата. Строки, а не enum: они лежат в колонке `preview_class`.
RELEVANT = "relevant"
OFF_TOPIC = "off_topic"
FOREIGN_CITY = "foreign_city"
UNKNOWN = "unknown"
CLASSES = (RELEVANT, OFF_TOPIC, FOREIGN_CITY, UNKNOWN)

# Рыночные слова: жильё, транспорт, барахолка на ru/en/vi. Перенесены из разведки
# 2026-10 (dry-run на очереди); словарь категорий из `search.market_terms`
# добавляется поверх в `search/chat_preview.py`, а не копируется сюда.
MARKET = re.compile(
    r"аренд|сда[мюёе]|снять|квартир|апартамент|жиль|комнат|дом[аы]? |вилл|недвиж|риелт|"
    r"барахол|купл|прода|объявлен|байк|мото|скутер|авто|машин|велосипед|"
    r"rent|apartment|flat|house|villa|room|real estate|property|bike|motorbike|scooter|car |"
    r"buy|sell|market|flea|classified|"
    r"cho thu[eê]|thu[eê] nh[aà]|căn hộ|can ho|nhà |phòng|phong tro|mua b[aá]n|xe m[aá]y|ô tô|"
    r"o to|b[aấ]t đ[oộ]ng s[aả]n",
    re.IGNORECASE,
)
# Явно другая тема. Применяется только к ОПИСАНИЮ: название вроде «Visa Run Group»
# без описания ничего не доказывает.
OFFTOPIC = re.compile(
    r"медиц|страхов|стоматолог|врач|клиник|здоров|пилатес|йог|фитнес|спорт|танц|"
    r"юрист|юридич|адвокат|нотари|виз[аы]|визаран|крипт|трейд|инвест|знакомств|"
    r"новост|погод|экскурс|тур[ыа ]|путешеств|рыбалк|дет[иск]|школ|образован|психолог|"
    r"церк|религ|музык|кино|игр[ыа]|бизнес-клуб|нетворк|"
    r"medic|insurance|dental|doctor|clinic|health|yoga|fitness|sport|lawyer|visa|crypto|"
    r"dating|news|weather|tour|travel|kids|school|church|music|game",
    re.IGNORECASE,
)
# Ссылки и @-контакты: слово внутри них - адрес автора, а не тема.
_LINKS = re.compile(r"(?:https?://|t\.me/|telegram\.me/)\S+|@\w+", re.IGNORECASE)
_META = r'<meta property="og:{prop}" content="([^"]*)"'
_EXTRA = re.compile(r'tgme_page_extra">([^<]*)')


@dataclass(frozen=True, slots=True)
class PreviewSnapshot:
    """Что видно на публичной странице. `status`: ok | no_page | unavailable."""

    status: str
    title: str = ""
    description: str = ""
    extra: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "title": self.title,
            "description": self.description,
            "extra": self.extra,
        }


def unavailable(reason: str) -> PreviewSnapshot:
    """Превью не получено (сеть, статус): это незнание, а не решение о чате."""
    return PreviewSnapshot(status="unavailable", extra=reason)


def parse_preview(page: str) -> PreviewSnapshot:
    """og:title / og:description / «N members»; без названия - страницы нет."""

    def meta(prop: str) -> str:
        found = re.search(_META.format(prop=prop), page)
        return html.unescape(found.group(1)).strip() if found else ""

    title = meta("title")
    if not title or title.startswith("Telegram: Contact"):
        return PreviewSnapshot(status="no_page")
    extra = _EXTRA.search(page)
    return PreviewSnapshot(
        status="ok",
        title=title,
        description=meta("description"),
        extra=html.unescape(extra.group(1)).strip() if extra else "",
    )


def strip_links(text: str) -> str:
    return _LINKS.sub(" ", text)


def matched(pattern: re.Pattern[str], text: str) -> list[str]:
    """Совпавшие маркеры: нижний регистр, без повторов, не больше четырёх."""
    return sorted({hit.strip().lower() for hit in pattern.findall(text)})[:4]
