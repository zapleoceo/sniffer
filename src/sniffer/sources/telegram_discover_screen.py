"""Отсев кандидата по тому, что видно без вступления.

Решение принимается по ответу `resolve_username`: тип сущности, название,
описание. Вступить, посмотреть и выйти — это два исходящих действия вместо
нуля, оба видны Telegram и оба считаются в суточный лимит.

Что отбрасываем и почему именно это:

- **канал** — в канале нет объявлений от людей, только вещание владельца
  (docs/chats-nha-trang.md, критерий отбора ручного списка);
- **бот и пользователь** — `@name` в тексте чаще всего человек или бот, а не
  чат; вступать туда некуда;
- **не наш город** — дананговская барахолка не ошибка, а шум: она зашумит
  выдачу и займёт место под потолком числа чатов. Пригодится при расширении на
  второй город (roadmap, P3), поэтому в журнал пишется причина, а не «нет».

Города различаются **данными**, а не ветвлением: второй город — это строка в
`CITY_MARKERS`, а не `if` в коде.
"""

from __future__ import annotations

import re

from sniffer.sources.telegram_discover_reference import (
    REJECT_BOT,
    REJECT_CHANNEL,
    REJECT_CITY_UNKNOWN,
    REJECT_FOREIGN_CITY,
    REJECT_USER,
    ResolvedChat,
)

# Как город называют в живых названиях групп. Формы взяты из реального списка
# (docs/chats-nha-trang.md): `nyachang`, `niachang`, `nhatrang` — всё это одна
# и та же Нячанг, и половина групп названа транслитом.
CITY_MARKERS: dict[str, tuple[str, ...]] = {
    "nha_trang": (
        "нячанг",
        "нячанге",
        "нхатранг",
        "nhatrang",
        "nyachang",
        "niachang",
        "nyachan",
        "nachang",
        "nyacang",
    ),
}

# Другие города Вьетнама. «Вьетнам» сюда не входит намеренно: `@auto_moto_vietnam`
# — это «Аренда Байков Нячанг», то есть страна в названии ничего не говорит о
# городе. Кам Рань в списке, хотя это та же провинция Кханьхоа: до неё полсотни
# километров, и байк оттуда клиенту не подходит (spec-v2, 4.1).
FOREIGN_CITY_MARKERS: tuple[str, ...] = (
    "дананг",
    "danang",
    "хошимин",
    "hochiminh",
    "сайгон",
    "saigon",
    "ханой",
    "hanoi",
    "фукуок",
    "phuquoc",
    "далат",
    "dalat",
    "вунгтау",
    "vungtau",
    "муйне",
    "muine",
    "хойан",
    "hoian",
    "фантьет",
    "phanthiet",
    "камрань",
    "camranh",
)

_NOT_ALNUM = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def screen(chat: ResolvedChat, *, city: str) -> str:
    """Причина отказа либо пустая строка, если кандидат наш."""
    # Бот проверяется первым: в Telegram бот — это пользователь, и обратный
    # порядок объяснил бы отказ по боту как «это человек».
    if chat.is_bot:
        return REJECT_BOT
    if chat.is_user:
        return REJECT_USER
    if not chat.is_group:
        return REJECT_CHANNEL
    haystack = _squash(" ".join((chat.username, chat.title, chat.about)))
    if _contains(haystack, CITY_MARKERS.get(city, ())):
        # Смешанный чат («Нячанг и Дананг») оставляем: наш город в нём есть,
        # а лишние объявления отсеет гейт. Отказ здесь стоил бы дороже ошибки.
        return ""
    if _contains(haystack, FOREIGN_CITY_MARKERS):
        return REJECT_FOREIGN_CITY
    # Города не видно вовсе. Не «наверное наш» — вступление стоит суток
    # ожидания и места под потолком, а перепроверить кандидата всегда можно
    # руками по журналу отклонённых.
    return REJECT_CITY_UNKNOWN


def _squash(text: str) -> str:
    """`Nha Trang`, `nha_trang` и `NhaTrang` — одно слово, а не три.

    Схлопываем всё, кроме букв и цифр: иначе список маркеров пришлось бы вести
    в каждой из форм, в которых название пишут в живых чатах.
    """
    return _NOT_ALNUM.sub("", text).lower()


def _contains(haystack: str, markers: tuple[str, ...]) -> bool:
    return any(marker in haystack for marker in markers)
