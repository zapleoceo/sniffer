"""Отпечаток объявления: что считается тем же постом, а что — другим."""

from __future__ import annotations

from sniffer.domain.fingerprint import fingerprint, normalized

SAME = [
    "🔥 Продам Honda Vision 2021!  Цена: 15.000.000 VND",
    "💥ПРОДАМ HONDA VISION 2021!!!\n\nЦена: 15.000.000 VND💥",
    "Продам   Honda Vision 2021 — цена 15.000.000 VND.",
]


def test_the_same_ad_with_other_decoration_is_one_ad() -> None:
    """Кросспост меняют эмодзи, регистр и переводы строк — но не объявление.

    Ровно это обещала схема с первого дня, и ровно этого не делал код: хеш
    считался от сырого текста, поэтому лишний смайлик заводил вторую карточку.
    """
    assert len({fingerprint(text) for text in SAME}) == 1


def test_a_different_year_is_a_different_ad() -> None:
    """Нормализация не смеет склеивать разные товары."""
    assert fingerprint("Honda Vision 2021, 15.000.000") != fingerprint(
        "Honda Vision 2022, 15.000.000"
    )


def test_a_different_price_is_a_different_ad() -> None:
    assert fingerprint("Honda Vision, 15.000.000") != fingerprint("Honda Vision, 17.000.000")


def test_vietnamese_diacritics_survive_normalisation() -> None:
    """`xe máy` не должно превратиться в `xe m y`.

    Соблазн написать список разрешённых букв (`a-zа-яё`) здесь велик и ошибочен:
    рынок трёхъязычный, и перечисление алфавитов означает однажды забыть чужой.
    """
    assert normalized("Bán xe máy Honda Vision") == "bán xe máy honda vision"


def test_an_empty_text_still_has_a_fingerprint() -> None:
    """Пустое сообщение до воронки не доходит, но падать на нём нечему."""
    assert fingerprint("") == fingerprint("   \n  ")
