"""Извлечение ссылок на чаты из сообщений, которые и так через нас проходят.

Перекрёстная ссылка — единственный способ узнать про группу, которой нет в
поиске: `contacts.SearchRequest` находит только то, что удачно названо, а
половина живых барахолок названа неудачно. Зато изнутри чата на них ссылаются
постоянно — «переходите в наш второй», «жильё у нас вот тут».

Формы, в которых ссылка встречается, и почему нужна каждая:

- `t.me/<username>` и `t.me/<username>/<msg>` — обычная ссылка и ссылка на
  пост; во второй имя чата тоже есть, терять его глупо;
- `t.me/joinchat/<hash>` и `t.me/+<hash>` — приглашение в закрытую группу,
  старая и новая формы одного и того же;
- `@username` прямо в тексте;
- `url` у сущности сообщения (`MessageEntityTextUrl`) — адрес спрятан за
  подписью и в тексте его нет вовсе. Это самая частая форма: разбор одного
  только текста пропустил бы её целиком.
"""

from __future__ import annotations

import re

from sniffer.sources.telegram_discover_reference import ChatCandidate, EntityLike, MessageLike

# Хвост ссылки после хоста. Останавливаемся на пробеле — обрезкой знаков
# препинания занимается разбор пути, здесь важно захватить путь целиком.
CHAT_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(\S+)", re.IGNORECASE)

# Упоминание в тексте. Отрицательный просмотр назад отсекает почту
# (`ivan@mail.ru`) и хвост ссылки: там `@` значит не то же самое.
MENTION = re.compile(r"(?<![\w/@.])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w@.])")

# Telegram: 5–32 символа, начинается с буквы, дальше буквы, цифры и подчёркивание.
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# Хэш приглашения — то, что Telegram выдаёт сам; длину не угадываем, а лишь
# отсекаем заведомо не-хэши вроде `t.me/+79001234567` (это номер телефона).
INVITE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Служебные пути t.me. `c` здесь ключевой: `t.me/c/1902334455/4471` — ссылка на
# пост в чате без username, вступить по ней нельзя, а `c` прошло бы за имя.
RESERVED = frozenset(
    {
        "joinchat",
        "c",
        "s",
        "share",
        "addstickers",
        "addemoji",
        "addtheme",
        "addlist",
        "setlanguage",
        "proxy",
        "socks",
        "login",
        "confirmphone",
        "bg",
        "invoice",
        "giftcode",
        "contact",
        "iv",
    }
)

# Знаки, которыми ссылку заканчивают в живом тексте: «зайди на t.me/chat,».
TRAILING = "\"'.,;:!?)]}»<>*_"


def candidates_from(message: MessageLike, found_in: str = "") -> list[ChatCandidate]:
    """Все кандидаты одного сообщения, без повторов, в порядке появления."""
    found: dict[str, ChatCandidate] = {}
    for candidate in _from_text(message.message or "", found_in):
        found.setdefault(candidate.key, candidate)
    for entity in message.entities or ():
        for candidate in _from_entity(entity, found_in):
            found.setdefault(candidate.key, candidate)
    return list(found.values())


def candidates_from_text(text: str, found_in: str = "") -> list[ChatCandidate]:
    """Тот же разбор для строки без сущностей — описание чата, шапка группы."""
    found: dict[str, ChatCandidate] = {}
    for candidate in _from_text(text, found_in):
        found.setdefault(candidate.key, candidate)
    return list(found.values())


def _from_text(text: str, found_in: str) -> list[ChatCandidate]:
    out: list[ChatCandidate] = []
    for path in CHAT_LINK.findall(text):
        if (candidate := _from_path(path, found_in)) is not None:
            out.append(candidate)
    for name in MENTION.findall(text):
        out.append(_username_candidate(name, found_in))
    return out


def _from_entity(entity: EntityLike, found_in: str) -> list[ChatCandidate]:
    """Ссылка из сущности. `url` есть только у `MessageEntityTextUrl`.

    У остальных сущностей адрес лежит в самом тексте и уже разобран: дважды
    один и тот же кандидат не мешает — повторы схлопываются по ключу.
    """
    url = getattr(entity, "url", None)
    if not isinstance(url, str) or not url:
        return []
    return _from_text(url, found_in)


def _from_path(path: str, found_in: str) -> ChatCandidate | None:
    """Путь после `t.me/` → кандидат."""
    path = path.split("?")[0].split("#")[0].rstrip(TRAILING)
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    head = parts[0]
    if head.lower() == "joinchat":
        return _invite_candidate(parts[1], found_in) if len(parts) > 1 else None
    if head.startswith("+"):
        return _invite_candidate(head[1:], found_in)
    if head.lower() in RESERVED or not USERNAME.match(head):
        return None
    return _username_candidate(head, found_in)


def _username_candidate(name: str, found_in: str) -> ChatCandidate:
    """Ключ в нижнем регистре: Telegram имена регистронезависимы."""
    name = name.lstrip("@")
    return ChatCandidate(key=f"@{name.lower()}", username=name, found_in=found_in)


def _invite_candidate(raw: str, found_in: str) -> ChatCandidate | None:
    """Регистр хэша сохраняем: `+AbC` и `+abc` — разные приглашения."""
    raw = raw.rstrip(TRAILING)
    if not INVITE.match(raw):
        return None
    return ChatCandidate(key=f"+{raw}", invite_hash=raw, found_in=found_in)
