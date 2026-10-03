"""Что бот говорит ВЛАДЕЛЬЦУ про оплату: что требует внимания и ответы на его команду `/refund`.

Отдельно от `billing_wording.py`, потому что у текстов разный адресат и разная причина
правок: клиентский текст — это обещание клиенту (условия, цена, отказы), а здесь —
служебные сообщения с идентификаторами платежей, которые клиенту не показываются.
Лист по импортам, как и клиентские формулировки: только `domain` и они сами.

Всё динамическое экранируется: бот шлёт HTML, а в тексте обращения или имени клиента
может оказаться что угодно.
"""

from __future__ import annotations

from html import escape

from sniffer.bot.billing_wording import why
from sniffer.domain.billing import Reason, StoredPayment


def _who(tg_user_id: int, username: str | None = None) -> str:
    tag = f" @{escape(username)}" if username else ""
    return f'<a href="tg://user?id={tg_user_id}">{tg_user_id}</a>{tag}'


def _code(value: str) -> str:
    return f"<code>{escape(value)}</code>"


def owner_payment_rejected(
    *,
    tg_user_id: int,
    reason: Reason,
    amount: int,
    currency: str,
    charge_id: str,
    payload: str,
    refund: str,
) -> str:
    return (
        f"⚠️ Платёж не принят: {why(reason)}. Клиент {_who(tg_user_id)}, "
        f"{amount} {escape(currency)}, charge {_code(charge_id)}, счёт {_code(payload)}. "
        f"Возврат: {escape(refund)}."
    )


def owner_payment_unrecorded(
    *, tg_user_id: int, amount: int, currency: str, charge_id: str, payload: str
) -> str:
    return (
        "🚨 Платёж не записан в журнал: база недоступна. Звёзды сняты. Клиент "
        f"{_who(tg_user_id)}, {amount} {escape(currency)}, charge {_code(charge_id)}, счёт "
        f"{_code(payload)}. Запишите платёж вручную или верните: /refund {escape(charge_id)}"
    )


def owner_payment_orphan(*, amount: int, currency: str, charge_id: str, payload: str) -> str:
    return (
        "🚨 Пришла оплата, у которой не определить плательщика: не записана. "
        f"{amount} {escape(currency)}, charge {_code(charge_id)}, счёт {_code(payload)}."
    )


def owner_refund_outside(*, tg_user_id: int, amount: int, charge_id: str) -> str:
    return (
        f"ℹ️ Возврат не через бота: charge {_code(charge_id)}, клиент {_who(tg_user_id)}, "
        f"{amount} ⭐. В журнале он отмечен."
    )


def owner_support(
    *, command: str, tg_user_id: int, username: str | None, text: str, payments: list[str]
) -> str:
    history = "\n".join(escape(line) for line in payments) or "платежей нет"
    return (
        f"💬 /{escape(command)} от {_who(tg_user_id, username)}:\n{escape(text)}\n\n"
        f"Последние платежи:\n{history}"
    )


def payment_line(payment: StoredPayment) -> str:
    """Строка о платеже для обращения: по ней владелец находит charge id и решает про возврат."""
    stamp = payment.created_at.strftime("%d.%m %H:%M")
    return (
        f"{stamp} UTC · {payment.amount} ⭐ · {payment.kind} · {payment.status} · "
        f"{payment.charge_id}"
    )


def refund_usage() -> str:
    return "Использование: /refund &lt;charge_id&gt; [telegram id клиента]"


def refund_not_in_ledger(charge_id: str) -> str:
    return (
        f"Платежа {_code(charge_id)} нет в журнале. Если он есть в Telegram, укажите id клиента: "
        "/refund &lt;charge_id&gt; &lt;id&gt;"
    )


def refund_done(charge_id: str, notes: list[str]) -> str:
    extra = "".join(f"\n• {escape(note)}" for note in notes)
    return f"Возврат выполнен: {_code(charge_id)}{extra}"


def refund_failed(charge_id: str, error: str) -> str:
    return f"Возврат {_code(charge_id)} не прошёл: {escape(error)}"


def owner_slots_unsynced(*, tg_user_id: int, charge_id: str) -> str:
    return (
        f"🚨 Платёж записан, но слот не включился: клиент {_who(tg_user_id)}, "
        f"charge {_code(charge_id)}. Клиенту сказано, что слот появится позже; сверка "
        "повторит пересчёт сама, но проверьте журнал."
    )


def owner_date_anomaly(*, tg_user_id: int, charge_id: str, expiration: int | None) -> str:
    return (
        f"⚠️ Telegram прислал странную дату окончания ({expiration}): клиент {_who(tg_user_id)}, "
        f"charge {_code(charge_id)}. Срок взят оценкой (30 суток), платёж записан."
    )


def owner_gap_unpaid(*, tg_user_id: int, amount: int, charge_id: str) -> str:
    return (
        f"🔎 Сверка: запись без платежа. В журнале платёж {_code(charge_id)} клиента "
        f"{_who(tg_user_id)} на {amount} ⭐ есть, в истории звёзд Telegram — нет. "
        "Слот при этом считается живым: проверьте платёж вручную."
    )


def owner_gap_recovered(*, tg_user_id: int, amount: int, charge_id: str, outcome: str) -> str:
    return (
        f"🔎 Сверка: платёж без записи. В истории Telegram есть {_code(charge_id)} от клиента "
        f"{_who(tg_user_id)} на {amount} ⭐, в журнале его не было. {escape(outcome)}"
    )


def owner_refund_stuck(*, tg_user_id: int, charge_id: str, error: str) -> str:
    return (
        f"🚨 Сверка не смогла довести возврат: charge {_code(charge_id)}, клиент "
        f"{_who(tg_user_id)}: {escape(error)}. Верните вручную: /refund {escape(charge_id)}"
    )
