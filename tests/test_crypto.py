"""Шифрование строки сессии в покое.

Строка сессии равна доступу к аккаунту, а дампы базы уезжают на диск по крону.
Поэтому проверяется не только «расшифровывается обратно», но и что открытый
текст в колонке отвергается, а слабый ключ не принимается молча.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import InvalidToken

from sniffer import crypto
from sniffer.config import reload_settings

KEY = "encryption-key-длинный-и-случайный-32+"
OTHER_KEY = "другой-ключ-тоже-достаточно-длинный-32+"
SESSION = "1BQANOTEuMTA4LjU2LjEyOAG7VERYSECRETSESSION"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", KEY)
    reload_settings()
    yield
    reload_settings()


def test_round_trip() -> None:
    assert crypto.decrypt(crypto.encrypt(SESSION)) == SESSION


def test_ciphertext_does_not_contain_the_secret() -> None:
    stored = crypto.encrypt(SESSION)

    assert SESSION not in stored
    assert stored.startswith(crypto.ENCRYPTED_PREFIX)


def test_same_secret_encrypts_differently_each_time() -> None:
    """Fernet солит: одинаковые строки не дают одинаковых шифртекстов."""
    assert crypto.encrypt(SESSION) != crypto.encrypt(SESSION)


def test_another_key_cannot_read_it() -> None:
    stored = crypto.encrypt(SESSION, key=KEY)

    with pytest.raises(InvalidToken):
        crypto.decrypt(stored, key=OTHER_KEY)


def test_plaintext_in_the_column_is_refused() -> None:
    """Закрывает вектор «кто-то с правом записи подложил своё значение»."""
    with pytest.raises(InvalidToken):
        crypto.decrypt(SESSION)


def test_weak_key_is_refused_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Слабый ключ работает так же, как сильный, — значит, ловить его надо на входе."""
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "коротко")
    reload_settings()

    assert not crypto.encryption_available()
    with pytest.raises(crypto.SecretsNotConfigured):
        crypto.encrypt(SESSION)


def test_missing_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "")
    reload_settings()

    assert not crypto.encryption_available()
    with pytest.raises(crypto.SecretsNotConfigured):
        crypto.encrypt(SESSION)


def test_tampered_ciphertext_is_refused() -> None:
    """У Fernet есть HMAC: изменённый байт ловится, а не расшифровывается в мусор."""
    stored = crypto.encrypt(SESSION)
    broken = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")

    with pytest.raises(InvalidToken):
        crypto.decrypt(broken)
