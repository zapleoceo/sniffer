"""Первоисточник объявления: где пост опубликован впервые.

Карточка обязана вести на оригинал — пост в том чате или канале, где объявление
появилось. Копия оригиналом не является, и таких копий три вида:

- **пересылка** (`fwd_from`). Telegram сохраняет текст дословно и помнит, откуда
  переслано: канал и номер поста (`channel_post`) или сохранённый пир
  (`saved_from_peer` + `saved_from_msg_id`). Тогда оригинал известен точно;
- **пересылка без адреса**: от пользователя, от скрытого автора (`from_name`),
  из группы без номера поста. Telegram не говорит, где пост лежит, поэтому
  первоисточник НЕ определён, и выдавать копию за оригинал нельзя;
- **указатель**: сообщение, которое по сути одна ссылка на чужой пост
  (`t.me/chat/123`) или на сокращатель/внешний адрес. Оригинал — то, на что
  ссылаются, а не сама ссылка.

Модуль без ввода-вывода: адрес сокращателя мы не раскрываем (сборщику запрещено
ходить по произвольным адресам), а значит такой указатель — «первоисточник не
определён», а не «вот он».
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

# Хосты Telegram, по которым открывается пост. `telegram.me` и `telegram.dog` —
# старые зеркала, ведут туда же, что и `t.me`.
TELEGRAM_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog"})

# Остаток текста без ссылок короче этого (в буквах и цифрах) — это не объявление,
# а подпись к ссылке: «👉», «смотрите тут», «актуально». Сухой прогон уборки на
# живой базе 16.11.2026: из 31 997 активных карточек архива указателем не
# признана ни одна — настоящие объявления правило не задевает.
POINTER_MAX_REMAINDER = 25

# Префикс, которым Telegram размечает id каналов и супергрупп: -100 + id.
_CHANNEL_PREFIX = -1_000_000_000_000

_USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
_POST_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?"
    rf"(?:(?P<user>{_USERNAME})|c/(?P<internal>\d+))/(?:(?P<topic>\d+)/)?(?P<msg>\d+)\b",
    re.IGNORECASE,
)
# Адрес: со схемой, с `www.` или голый `host.tld/путь`. Домен верхнего уровня —
# латинские буквы, иначе «8.500.000đ/tháng» сошло бы за ссылку.
_ANY_LINK = re.compile(r"(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*", re.IGNORECASE)
# Ссылка на карту — место объекта внутри объявления, а не адрес другого объявления.
_MAP_HOSTS = re.compile(
    r"^(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[a-z.]+/maps|maps\.google\.)",
    re.IGNORECASE,
)
_HANDLE = re.compile(r"@[A-Za-z0-9_]{4,32}")
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)


class OriginKind(StrEnum):
    """Что известно о первоисточнике сообщения. `None` в базе — сообщение само оригинал."""

    FORWARD = "forward"  # пересылка, адрес оригинала известен
    FORWARD_HIDDEN = "forward_hidden"  # пересылка, адреса нет — не первоисточник
    POINTER = "pointer"  # сообщение — ссылка на чужой пост или внешний адрес


# Сообщения этих видов никогда не становятся карточкой со своей ссылкой.
NOT_PRIMARY = frozenset({OriginKind.FORWARD_HIDDEN, OriginKind.POINTER})


@dataclass(frozen=True, slots=True)
class PostRef:
    """Адрес поста. Хватит одного из `chat_tg_id` / `username`, номер обязателен."""

    msg_id: int
    chat_tg_id: int | None = None
    username: str | None = None

    @property
    def url(self) -> str | None:
        """Публичная ссылка на пост или `None`, если имени чата нет.

        Форма `t.me/c/<id>/<msg>` открывается только участникам чата — клиенту
        такая ссылка бесполезна, поэтому без имени первоисточник не ссылаем.
        """
        if not self.username:
            return None
        return f"https://t.me/{self.username.lstrip('@')}/{self.msg_id}"


@dataclass(frozen=True, slots=True)
class Origin:
    kind: OriginKind | None = None
    post: PostRef | None = None

    @property
    def primary(self) -> bool:
        """Можно ли ссылаться: оригинал сам по себе или пересылка с известным адресом."""
        return self.kind is None or (self.kind is OriginKind.FORWARD and self.url is not None)

    @property
    def url(self) -> str | None:
        return self.post.url if self.post is not None else None


OWN_POST = Origin()


def marked_channel_id(channel_id: int) -> int:
    """Id канала в размеченной форме `-100…`, как в `chats.tg_id`."""
    return _CHANNEL_PREFIX - channel_id


def forward_origin(message: object) -> Origin:
    """Откуда переслано сообщение Telethon. Без `fwd_from` — сообщение само оригинал.

    Атрибуты читаются через `getattr`: протокол сообщения у адаптера узкий, а
    пересылка — необязательная часть ответа Telegram.
    """
    header = getattr(message, "fwd_from", None)
    if header is None:
        return OWN_POST
    for peer, post in (
        (getattr(header, "from_id", None), getattr(header, "channel_post", None)),
        (getattr(header, "saved_from_peer", None), getattr(header, "saved_from_msg_id", None)),
    ):
        channel_id = getattr(peer, "channel_id", None)
        if isinstance(channel_id, int) and isinstance(post, int) and post > 0:
            return Origin(
                OriginKind.FORWARD,
                PostRef(post, marked_channel_id(channel_id), _forward_username(message)),
            )
    return Origin(OriginKind.FORWARD_HIDDEN)


def _forward_username(message: object) -> str | None:
    """Имя канала-источника из сущностей ответа, без отдельного запроса к Telegram."""
    chat = getattr(getattr(message, "forward", None), "chat", None)
    username = getattr(chat, "username", None)
    return username if isinstance(username, str) and re.fullmatch(_USERNAME, username) else None


def pointer_origin(text: str) -> Origin | None:
    """Указатель ли это: ссылка и почти ничего вокруг. `None` — обычный текст.

    Ссылка на пост Telegram даёт адрес оригинала. Сокращатель, внешний сайт или
    ссылка на чат без номера поста — первоисточник не определён (`post=None`).
    Ссылки на карты внутри настоящего объявления сюда не попадают: у такого
    текста остаток длинный, и это оригинал.
    """
    posts = {_post_ref(match) for match in _POST_LINK.finditer(text)}
    if not posts and not any(_points_away(link) for link in _ANY_LINK.findall(text)):
        # Ссылка на чат без номера поста и @имя — это контакт автора, а не указатель.
        return None
    remainder = _HANDLE.sub(" ", _ANY_LINK.sub(" ", _POST_LINK.sub(" ", text)))
    if len(_ALNUM.findall(remainder)) >= POINTER_MAX_REMAINDER:
        return None
    if len(posts) == 1:
        return Origin(OriginKind.POINTER, posts.pop())
    return Origin(OriginKind.POINTER)


def _points_away(link: str) -> bool:
    """Внешний адрес, за которым может стоять другое объявление (сокращатель, сайт)."""
    bare = re.sub(r"^(?:https?://)", "", link, flags=re.IGNORECASE)
    host = bare.split("/", 1)[0].lower()
    return host not in TELEGRAM_HOSTS and _MAP_HOSTS.match(bare) is None


def _post_ref(match: re.Match[str]) -> PostRef:
    msg_id = int(match.group("msg"))
    if match.group("user"):
        return PostRef(msg_id, username=match.group("user"))
    return PostRef(msg_id, chat_tg_id=marked_channel_id(int(match.group("internal"))))


def message_origin(message: object, text: str) -> Origin:
    """Первоисточник сообщения: сначала пересылка, потом указатель."""
    forwarded = forward_origin(message)
    if forwarded.kind is not None:
        return forwarded
    return pointer_origin(text) or OWN_POST


def canonical_telegram_url(url: str) -> str | None:
    """Одна форма ссылки на пост: `https://t.me/<имя>/<номер>`.

    Зеркала, `http`, `/s/`, `?single` и хвостовой слэш — одна и та же публикация.
    Регистр имени сохраняется: Telegram к нему безразличен, и переписывать
    тысячи рабочих ссылок ради регистра незачем (замер 16.11.2026: 5588 карточек
    отличались бы только им). Сравнивать ссылки — через `same_post_key`.
    Тема форума сохраняется: без неё ссылка в форуме не открывается.
    `None` — это не ссылка на пост Telegram.
    """
    parsed = urlsplit(url.strip() if "://" in url else f"https://{url.strip()}")
    if (parsed.hostname or "").lower() not in TELEGRAM_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts[:1] == ["s"]:
        parts = parts[1:]
    if len(parts) < 2 or not all(part.isdigit() for part in parts[1:]) or len(parts) > 4:
        return None
    if parts[0] == "c":
        if len(parts) < 3:
            return None
        return "https://t.me/c/" + "/".join(parts[1:])
    if not re.fullmatch(_USERNAME, parts[0]):
        return None
    return "https://t.me/" + "/".join(parts)


def same_post_key(url: str) -> str:
    """Ключ сравнения ссылок: каноническая форма без учёта регистра имени."""
    return (canonical_telegram_url(url) or url.strip()).lower()
