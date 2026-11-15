"""Ключи «это то же объявление» для дедупа на входе и при уборке каталога.

Одна квартира приходит много раз: кросспост в пять групп, пересылка из канала,
перепост того же текста с новым номером сообщения — и перепост с новой ценой.
Первые три ловит отпечаток текста (`domain.fingerprint`). Последний — нет:
отпечаток намеренно чувствителен к цифрам, «Honda 125» и «Honda 150» обязаны
различаться.

Ключ личности (`identity_hash`) — отпечаток текста, из которого вырезаны ТОЛЬКО
суммы, которые разбор цен (`domain.price_facts`) признал ценой. Площадь, этаж,
кубатура, год и телефон остаются: перепост с новой ценой сходится, соседняя
квартира того же агентства с другой площадью — нет.

Короткий текст ключа не получает: «Сдам студию, 6 млн» без цены превращается в
два слова, и такие тексты у разных людей совпадают сами собой.
"""

from __future__ import annotations

import hashlib

from sniffer.domain.fingerprint import fingerprint, normalized
from sniffer.domain.origin import same_post_key
from sniffer.domain.price_facts import parse_prices

# Нормализованный остаток без цены короче этого — ключа нет: однострочники вроде
# «Сдам студию, тел. …» у разных людей совпадают сами собой. Порог осторожный и
# выбран, а не выведен замером; сухой прогон уборки показывает, что он даёт.
IDENTITY_MIN_CHARS = 60
_VERSION = "v1"


def identity_text(text: str) -> str:
    """Нормализованный текст без найденных в нём цен."""
    stripped = text
    # Длинные сначала: «15.000.000 VND» не должен потерять только «15.000.000».
    for raw in sorted({fact.raw for fact in parse_prices(text) if fact.raw}, key=len, reverse=True):
        stripped = stripped.replace(raw, " ")
    return normalized(stripped)


def identity_hash(text: str) -> str | None:
    """Ключ объявления без учёта цены или `None` для слишком короткого текста."""
    body = identity_text(text)
    if len(body) < IDENTITY_MIN_CHARS:
        return None
    return hashlib.sha256(f"{_VERSION}|{body}".encode()).hexdigest()


def identity_keys(url: str, text: str, *, textual: bool) -> tuple[str, ...]:
    """Все ключи наблюдения каталога, по любому из которых находится дубль.

    `url` — ссылка на первоисточник: копии одной пересылки сходятся по ней.
    `textual` — источник отдаёт текст объявления (Telegram); у Chotot текст
    содержит JSON с номером лота, и текстовые ключи там ничего не дают.
    """
    keys = [f"url:{same_post_key(url)}"]
    if textual:
        keys.append(f"text:{fingerprint(text)}")
        if (near := identity_hash(text)) is not None:
            keys.append(f"near:{near}")
    return tuple(keys)
