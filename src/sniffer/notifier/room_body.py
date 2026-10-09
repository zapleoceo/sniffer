"""Текст сообщения для комнаты: всё, что агенту нужно, чтобы проверить вариант глазами.

Чистая функция без ввода-вывода. Контактов сверх текста объявления не добавляет: то, что в
самом посте, остаётся в тексте, а своих номеров и имён продавца здесь нет.
"""

from __future__ import annotations

from datetime import datetime

from sniffer.domain.room_relay import RelayCandidate

# Лимит `room_post.body` на стороне комнаты — 20 000 знаков. Берём с запасом на шапку.
MAX_TEXT_CHARS = 18_000
UNKNOWN = "по тексту / неизвестно"
NO_TEXT = "(исходный текст поста недоступен; ниже краткое содержание)"


def photo_link(tg_link: str) -> str:
    """Публичный превью поста с фото (виджет Telegram)."""
    return f"{tg_link}{'&' if '?' in tg_link else '?'}embed=1"


def format_price(item: RelayCandidate) -> str:
    if item.price_amount is None:
        return "цена не указана"
    amount = item.price_amount
    shown = f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"
    parts = [shown.replace(",", " "), item.price_currency or "", item.price_period or ""]
    return " ".join(p for p in parts if p)


def freshness(posted_at: datetime, now: datetime) -> str:
    days = max((now - posted_at).days, 0)
    return f"{posted_at:%Y-%m-%d} ({days} дн. назад)"


def kitchen(item: RelayCandidate) -> str:
    value = item.attributes.get("kitchen")
    if value == "separate":
        return "отдельная"
    if value == "shared":
        return "совмещённая"
    return UNKNOWN


def balcony(item: RelayCandidate) -> str:
    value = item.attributes.get("balcony")
    if isinstance(value, bool):
        return "есть" if value else "нет"
    return UNKNOWN


def place(item: RelayCandidate) -> str:
    return ", ".join(p for p in (item.district, item.city) if p)


def build_body(item: RelayCandidate, now: datetime) -> str:
    media = "есть" if item.has_media else "нет"
    text = (item.text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "…"
    lines = [
        f"Кандидат слежения #{item.notification_id} (подписка {item.subscription_id}, "
        f"score {item.score:.2f}): проверь по фото и тексту.",
        f"Пост: {item.tg_link}",
        f"Фото (превью с картинками, медиа в посте: {media}): {photo_link(item.tg_link)}",
        f"Опубликовано: {freshness(item.posted_at, now)}",
        f"Цена: {format_price(item)}",
        f"Район/город: {place(item) or UNKNOWN}",
        f"Кухня: {kitchen(item)}",
        f"Балкон: {balcony(item)}",
        f"Заголовок: {item.title}",
        "",
        "Текст объявления:",
        text or f"{NO_TEXT}\n{item.summary}",
    ]
    return "\n".join(lines)
