"""Шифрование секретов в покое — Fernet (AES-128-CBC + HMAC).

В проекте ровно один секрет живёт в хранилище: строка сессии юзербота. Она
равна доступу к аккаунту, а дампы базы уезжают на диск по крону
(deploy.md, раздел 10), поэтому в базу она кладётся шифртекстом.

Ключ берётся из `SECRET_ENCRYPTION_KEY` и НЕ совпадает ни с ключом подписи
cookie, ни с `BOT_TOKEN`: у трёх разных задач три разных ключа, чтобы утечка
одного не обесценивала остальные.

Открытого текста в колонке быть не может: без префикса `enc1:` расшифровка
отказывает, а не «пробует как есть». Это закрывает вектор «кто-то с правом
записи в базу подложил своё значение».
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from sniffer.config import get_settings

# Метка версии алгоритма: когда придёт enc2, старые строки останутся читаемыми.
ENCRYPTED_PREFIX = "enc1:"

# Меньше — это уже не секрет, а пароль от wifi. Проверяем на входе, потому что
# слабый ключ здесь выглядит точно так же, как сильный, и молча работает.
MIN_KEY_LEN = 32


class SecretsNotConfigured(RuntimeError):
    """Ключа шифрования нет или он слишком короткий."""


def _fernet(key: str | None = None) -> Fernet:
    secret = (key if key is not None else get_settings().secret_encryption_key).strip()
    if len(secret) < MIN_KEY_LEN:
        raise SecretsNotConfigured(
            f"SECRET_ENCRYPTION_KEY короче {MIN_KEY_LEN} символов — отказываюсь шифровать слабо"
        )
    # Fernet требует 32 байта в urlsafe-base64; произвольную строку приводим
    # sha256, а не обрезанием: обрезание молча теряет энтропию.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str, *, key: str | None = None) -> str:
    """Секрет → `enc1:<шифртекст>`."""
    return f"{ENCRYPTED_PREFIX}{_fernet(key).encrypt(plain.encode('utf-8')).decode('ascii')}"


def decrypt(stored: str, *, key: str | None = None) -> str:
    """`enc1:<шифртекст>` → секрет. Открытый текст в колонке — ошибка."""
    if not stored.startswith(ENCRYPTED_PREFIX):
        raise InvalidToken(
            "в колонке не шифртекст (нет префикса enc1:): либо ключ сменился, "
            "либо строку подменили в обход приложения"
        )
    payload = stored[len(ENCRYPTED_PREFIX) :]
    return _fernet(key).decrypt(payload.encode("ascii")).decode("utf-8")


def encryption_available() -> bool:
    """Можно ли вообще шифровать. Дашборду нужно знать это до формы, а не после."""
    return len(get_settings().secret_encryption_key.strip()) >= MIN_KEY_LEN
