"""Вход владельца: Telegram Login Widget → подписанная cookie.

Три ключа, три задачи, и они не пересекаются:

- **подпись виджета** проверяется ключом `sha256(BOT_TOKEN)` — так требует
  Telegram, и владеть им может только тот, у кого есть токен бота;
- **наша cookie** подписывается `DASHBOARD_SESSION_SECRET`;
- **строка сессии юзербота** шифруется `SECRET_ENCRYPTION_KEY` (`crypto.py`).

Один ключ на две задачи означал бы, что утечка одной обесценивает обе, поэтому
переиспользования здесь нет нигде.

Что закрыто и чем:

| Атака | Чем закрыта |
|---|---|
| подделка cookie | HMAC-SHA256, сравнение `compare_digest` |
| тайминг-атака на подпись | `compare_digest`, никогда `==` |
| повтор параметров виджета | окно `auth_date` 5 минут + одноразовость `hash` |
| смена владельца | id владельца внутри подписанного payload |
| чужой аккаунт | сверка с `OWNER_CHAT_ID`, ответ 403, а не пустая страница |
| CSRF на форме | подписанный токен с отдельным префиксом + `SameSite=Lax` |
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

import structlog

from sniffer.config import Settings, get_settings

log = structlog.get_logger(__name__)

COOKIE_NAME = "sniffer_owner"
# Тридцать суток: страницу открывает один человек с одного ноутбука, и
# ежедневный повторный вход виджетом не добавляет безопасности, только трение.
SESSION_TTL_S = 30 * 24 * 60 * 60
# Окно свежести подписи виджета. Пять минут, а не сутки: перехваченный набор
# параметров иначе работает почти день.
WIDGET_AUTH_TTL_S = 300
# Часы клиента и сервера расходятся; секунды в будущее допускаем, минуты — нет.
CLOCK_SKEW_S = 60
CSRF_TTL_S = 60 * 60

# Префиксы разделяют назначения подписи. Без них токен CSRF, подписанный тем же
# секретом, годился бы как cookie сессии.
_SESSION_KIND = "owner"
_CSRF_KIND = "csrf"

# Одноразовость подписи виджета: внутри окна свежести один и тот же набор
# параметров принимается ровно однажды. Память процесса тут достаточна — окно
# короче любого разумного рестарта, а cookie переживает его и так.
_used_widget_hashes: dict[str, float] = {}


class AuthError(Exception):
    """Вход не удался. `status` — что отдать наружу."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def owner_id() -> int:
    return get_settings().owner_chat_id


def missing_settings(settings: Settings | None = None) -> list[str]:
    """Чего не хватает, чтобы вход вообще работал. Именами из `.env`.

    `BOT_TOKEN` проверяет подпись виджета, `DASHBOARD_SESSION_SECRET` подписывает
    cookie, `OWNER_CHAT_ID` говорит, кого пускать. Не хватает любого — пускать
    некого и нечем, а открытая страница была бы хуже закрытой.
    """
    settings = settings or get_settings()
    missing = []
    if not settings.bot_token.strip():
        missing.append("BOT_TOKEN")
    if not settings.dashboard_session_secret.strip():
        missing.append("DASHBOARD_SESSION_SECRET")
    if not settings.owner_chat_id:
        missing.append("OWNER_CHAT_ID")
    return missing


def _digest(body: str) -> str:
    secret = get_settings().dashboard_session_secret
    if not secret.strip():
        # Пустой секрет подписал бы всё и проверил бы всё: это не «работает без
        # настройки», это открытая дверь.
        raise AuthError(503, "DASHBOARD_SESSION_SECRET не задан — вход выключен")
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _sign(kind: str, *, now: float | None = None) -> str:
    """Токен `<kind>:<owner>:<issued>.<подпись>`.

    Владелец внутри payload не для красоты: сменили `OWNER_CHAT_ID` — все ранее
    выпущенные токены перестают действовать сами, без списка отзыва.
    """
    issued = int(time.time() if now is None else now)
    body = f"{kind}:{owner_id()}:{issued}"
    return f"{body}.{_digest(body)}"


def _verify(kind: str, token: str | None, ttl_s: int) -> bool:
    """Подпись верна, вид тот, срок не вышел, владелец тот же."""
    if not token or "." not in token:
        return False
    body, signature = token.rsplit(".", 1)
    parts = body.split(":")
    if len(parts) != 3 or parts[0] != kind:
        return False
    try:
        expected = _digest(body)
    except AuthError:
        return False
    # compare_digest, а не ==: посимвольное сравнение утекает позицию первого
    # различия через время ответа, и подпись подбирается побайтово.
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        owner_in_token, issued = int(parts[1]), int(parts[2])
    except ValueError:
        return False
    if owner_id() == 0 or owner_in_token != owner_id():
        return False
    return time.time() - issued <= ttl_s


def issue_session() -> tuple[str, int]:
    """Cookie владельца и её срок жизни в секундах."""
    return _sign(_SESSION_KIND), SESSION_TTL_S


def is_owner_session(cookie: str | None) -> bool:
    return _verify(_SESSION_KIND, cookie, SESSION_TTL_S)


def issue_csrf() -> str:
    return _sign(_CSRF_KIND)


def is_valid_csrf(token: str | None) -> bool:
    return _verify(_CSRF_KIND, token, CSRF_TTL_S)


def verify_widget(data: Mapping[str, str], *, now: float | None = None) -> int:
    """Данные Telegram Login Widget → id пользователя. Иначе `AuthError`.

    Подпись строится ключом `sha256(BOT_TOKEN)` по строке `k=v`, отсортированной
    по ключам, без самого `hash` — так описано у Telegram.
    """
    settings = get_settings()
    if not settings.bot_token.strip():
        raise AuthError(503, "BOT_TOKEN не задан — проверить подпись виджета нечем")

    received = str(data.get("hash", ""))
    if not received:
        raise AuthError(400, "в ответе виджета нет подписи")

    fields = {key: str(value) for key, value in data.items() if key != "hash"}
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(settings.bot_token.encode()).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError(403, "подпись виджета неверна")

    moment = time.time() if now is None else now
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError as exc:
        raise AuthError(400, "auth_date не число") from exc
    age = moment - auth_date
    if age > WIDGET_AUTH_TTL_S:
        raise AuthError(403, "подпись виджета просрочена")
    if age < -CLOCK_SKEW_S:
        raise AuthError(403, "auth_date из будущего")

    _consume_widget_hash(received, moment)

    try:
        return int(fields["id"])
    except (KeyError, ValueError) as exc:
        raise AuthError(400, "в ответе виджета нет id") from exc


def _consume_widget_hash(widget_hash: str, moment: float) -> None:
    """Один и тот же набор параметров принимается однажды."""
    for old, seen_at in list(_used_widget_hashes.items()):
        if moment - seen_at > WIDGET_AUTH_TTL_S + CLOCK_SKEW_S:
            _used_widget_hashes.pop(old, None)
    if widget_hash in _used_widget_hashes:
        raise AuthError(403, "эта подпись виджета уже использована")
    _used_widget_hashes[widget_hash] = moment


def authorize_widget(data: Mapping[str, str]) -> int:
    """Проверить виджет И что это владелец. Чужой аккаунт получает 403."""
    user_id = verify_widget(data)
    if user_id != owner_id() or owner_id() == 0:
        log.warning("dashboard.foreign_login", tg_user_id=user_id)
        raise AuthError(403, "этот аккаунт не владелец — доступ закрыт")
    return user_id
