"""Что бот говорит про подписку и оплату: длины, правдивость, одна цена на все тексты.

Пределы Telegram здесь не из памяти: `title` 1–32 символа, `description` 1–255, `payload`
1–128 байт, описание команды до 256 (справочник Bot API, `sendInvoice`, `BotCommand`).
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_wording as words
from sniffer.domain import plans
from sniffer.domain.billing import Reason, StoredPayment
from tests.test_thread_menu import assert_telegram_html

SRC = Path(__file__).resolve().parents[1] / "src" / "sniffer"

# История версий условий: версия → хэш того самого текста. Клиент соглашается с конкретным
# текстом, и правка текста без новой версии подменила бы то, с чем он согласился. Старые
# строки не правятся: новая версия — новая строка (и новая `TERMS_VERSION`).
KNOWN_TERMS = {
    "2026-10-03": "56cba0f0cfe5b9ac36c0d3ce03680f04e1096a9c814fe4faaef94b7bb8fa0aac",
}


def client_texts() -> list[str]:
    """Всё, что клиент может прочитать: константы и результаты функций."""
    until = datetime(2026, 11, 1, tzinfo=UTC)
    return [
        words.SUBSCRIBE_LABEL,
        words.ACCEPT_LABEL,
        words.TERMS_LABEL,
        words.CANCEL_LABEL,
        words.PAY_LABEL,
        words.INVOICE_LABEL,
        words.OFFER,
        words.ALREADY_ISSUED,
        words.STALE_BUTTON,
        words.BILLING_OFF,
        words.UNAVAILABLE,
        words.CANCELLED,
        words.TERMS_CHANGED,
        words.SUPPORT_THROTTLED,
        words.SUPPORT_UNAVAILABLE,
        words.invoice_title(2),
        words.invoice_description(),
        words.confirmation(0),
        words.confirmation(1),
        words.confirmation(3),
        words.link_ready(2),
        words.thanks_first(until),
        words.thanks_renewal(until),
        words.terms(48),
        words.paysupport_prompt(48),
        words.support_prompt(48),
        words.support_sent(48),
        words.payment_refund_failed(48),
        words.payment_unrecorded(48),
        words.subscription_refunded(),
        *(words.refusal(reason) for reason in Reason),
        *(words.payment_refunded(reason) for reason in Reason),
    ]


# ── пределы Telegram ────────────────────────────────────────────────────────


@pytest.mark.parametrize("number", [1, 2, 9, 99, 999, 99_999])
def test_the_invoice_title_fits_thirty_two_characters(number: int) -> None:
    assert 1 <= len(words.invoice_title(number)) <= 32


def test_the_invoice_description_fits_two_hundred_fifty_five_characters() -> None:
    assert 1 <= len(words.invoice_description()) <= 255


def test_the_invoice_description_points_to_the_terms_and_the_payment_support() -> None:
    """Чек-лист Telegram: условия и канал поддержки доступны в момент покупки."""
    description = words.invoice_description()

    assert "/terms" in description and "/paysupport" in description


def test_every_message_fits_a_telegram_message_and_is_valid_html() -> None:
    for text in client_texts():
        assert 0 < len(text) <= 4096, text[:60]
        assert_telegram_html(text)


@pytest.mark.parametrize(("command", "description"), words.COMMANDS)
def test_the_command_menu_follows_the_bot_api_rules(command: str, description: str) -> None:
    assert re.fullmatch(r"[a-z0-9_]{1,32}", command)
    assert 1 <= len(description) <= 256


def test_the_menu_has_the_commands_telegram_requires_of_a_paid_bot() -> None:
    names = [name for name, _ in words.COMMANDS]

    assert len(names) == len(set(names))
    assert {"subscription", "terms", "support", "paysupport"} <= set(names)


# ── правдивость ─────────────────────────────────────────────────────────────


def test_every_refusal_reason_has_words_for_the_client() -> None:
    """Новая причина отказа без текста упала бы KeyError прямо в окне `pre_checkout`."""
    for reason in Reason:
        assert words.refusal(reason)
        assert len(words.refusal(reason)) <= 200, "Telegram показывает это в окне оплаты"
        assert words.payment_refunded(reason)


@pytest.mark.parametrize(
    "text", [words.terms(48), words.paysupport_prompt(48), words.support_prompt(48)]
)
def test_the_client_is_told_that_telegram_support_will_not_help_with_purchases(text: str) -> None:
    """Требование чек-листа Telegram: обязательно сообщить об этом пользователям."""
    assert "Поддержка Telegram" in text


def test_the_texts_use_the_one_word_for_a_search_not_requests_or_branches() -> None:
    """Одно слово на одну вещь: человек видит «поиски» (см. тест меню поисков)."""
    for text in client_texts():
        lowered = text.lower()
        assert "ветк" not in lowered, text[:60]
        assert "запрос" not in lowered, text[:60]


def test_the_terms_say_what_is_sold_how_to_cancel_and_when_stars_come_back() -> None:
    text = words.terms(48)

    for needle in (
        "Что продаётся",
        "Автопродление и отмена",
        "Возвраты",
        "/paysupport",
        "/support",
    ):
        assert needle in text
    assert str(plans.SUBSCRIPTION_STARS) in text and str(plans.PAID_CARDS_CAP) in text
    assert words.TERMS_VERSION in text


def test_the_confirmation_counts_the_subscriptions_by_the_number() -> None:
    assert "1 подписка (1 слот слежения)" in words.confirmation(1)
    assert "2 подписки (2 слота слежения)" in words.confirmation(2)
    assert "5 подписок (5 слотов слежения)" in words.confirmation(5)
    assert f"Лимит карточек ({plans.PAID_CARDS_CAP}) не изменится" in words.confirmation(1)


# ── версия условий ──────────────────────────────────────────────────────────


def test_the_terms_version_is_the_version_of_this_exact_text() -> None:
    """Правка текста условий без новой версии подменила бы то, с чем клиент согласился.

    Изменили текст — добавьте новую `TERMS_VERSION` и новую строку в `KNOWN_TERMS` (старые не
    трогаем): клиенты, согласившиеся с прежним, согласятся с новым заново на экране «Подписка».
    """
    digest = hashlib.sha256(words.terms(48).encode()).hexdigest()

    assert KNOWN_TERMS.get(words.TERMS_VERSION) == digest, (
        f"текст условий изменился без новой версии: версия {words.TERMS_VERSION}, "
        f"хэш текста {digest}"
    )


def test_the_terms_version_is_a_date_the_payload_can_carry() -> None:
    assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", words.TERMS_VERSION)


# ── одна цена на всё ────────────────────────────────────────────────────────


def test_the_price_comes_from_the_single_constant_into_every_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Подмена тарифа доходит до каждого текста, который строится при вызове."""
    monkeypatch.setattr(plans, "SUBSCRIPTION_STARS", 77)

    for text in (
        words.invoice_description(),
        words.confirmation(0),
        words.confirmation(2),
        words.link_ready(1),
        words.terms(48),
    ):
        assert "77 ⭐" in text, text[:80]
        assert "10 ⭐" not in text, text[:80]


def test_the_price_comes_from_the_single_constant_into_the_labels() -> None:
    """Подписи кнопок строятся при импорте, поэтому проверяются перезагрузкой модуля."""
    original = plans.SUBSCRIPTION_STARS
    try:
        plans.SUBSCRIPTION_STARS = 77
        importlib.reload(words)
        assert "77 ⭐" in words.SUBSCRIBE_LABEL
        assert "77 ⭐" in words.PAY_LABEL
        assert "77 ⭐" in words.OFFER
    finally:
        plans.SUBSCRIPTION_STARS = original
        importlib.reload(words)
    assert f"{original} ⭐" in words.SUBSCRIBE_LABEL


def _string_literals(path: Path) -> list[str]:
    """Строковые литералы файла, кроме докстрингов: то, что может попасть к клиенту."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_source_file_writes_the_price_or_the_cap_as_a_number_in_a_string() -> None:
    """Рассинхрон кнопки и счёта — тёмный паттерн: число живёт в `domain/plans.py` и больше нигде.

    Ловит ровно то, что было раньше: «1 ⭐/мес» на кнопке и «Одна звезда в месяц» в счёте.
    """
    files = [*SRC.glob("bot/billing*.py"), SRC / "bot" / "handlers" / "billing.py"]
    files += [SRC / "bot" / "keyboards.py"]
    offenders = [
        f"{path.name}: {text[:50]!r}"
        for path in files
        for text in _string_literals(path)
        if re.search(rf"[0-9]+\s*⭐|⭐\s*[0-9]+|\b{plans.PAID_CARDS_CAP}\b", text)
    ]

    assert files and not offenders, offenders


# ── вспомогательные слова ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 час"),
        (2, "2 часа"),
        (4, "4 часа"),
        (5, "5 часов"),
        (11, "11 часов"),
        (12, "12 часов"),
        (14, "14 часов"),
        (21, "21 час"),
        (22, "22 часа"),
        (25, "25 часов"),
        (48, "48 часов"),
        (101, "101 час"),
        (111, "111 часов"),
    ],
)
def test_the_hours_are_declined_by_the_number(count: int, expected: str) -> None:
    assert words.hours(count) == expected


def test_the_date_is_the_vietnamese_calendar_date_in_russian() -> None:
    """18:30 UTC первого ноября — уже второе число во Вьетнаме (UTC+7)."""
    assert words.date_ru(datetime(2026, 11, 1, 18, 30, tzinfo=UTC)) == "2 ноября 2026"
    assert words.date_ru(datetime(2026, 1, 31, 16, 59, tzinfo=UTC)) == "31 января 2026"
    assert words.date_ru(datetime(2026, 12, 31, 17, 0, tzinfo=UTC)) == "1 января 2027"


# ── владельцу ───────────────────────────────────────────────────────────────


def test_what_a_client_wrote_cannot_break_the_owners_message() -> None:
    """Бот шлёт HTML, а обращение и имя клиента — чужой текст."""
    stored = StoredPayment(
        charge_id="c<1>&",
        tg_user_id=42,
        amount=10,
        currency="XTR",
        kind="first",
        status="paid",
        invoice_payload="p",
        is_recurring=True,
        is_first_recurring=True,
        period_end=None,
        refunded_at=None,
        created_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    )

    message = owner_words.owner_support(
        command="support",
        tg_user_id=42,
        username="<i>x</i>",
        text="<script>alert(1)</script> & ещё",
        payments=[owner_words.payment_line(stored)],
    )

    assert "<script>" not in message and "<i>x</i>" not in message
    assert_telegram_html(message)
    assert_telegram_html(owner_words.refund_done("c<1>&", ["a < b"]))
    assert_telegram_html(owner_words.refund_failed("c<1>&", "Bad <Request>"))
    assert_telegram_html(owner_words.refund_usage())
