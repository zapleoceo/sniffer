"""Подписка за звёзды Telegram: счёт, проверка перед оплатой, зачисление.

Что продаётся. Поиск остаётся бесплатным; за звезду в месяц бот продолжает
следить за темой запроса и присылает НОВЫЕ объявления по мере появления.
Предложение возникает там, где оно честно: когда из показанного клиент ничего
не выбрал, либо когда не нашлось ничего вовсе.

Почему именно звёзды и почему одна. Провайдер выбран владельцем 31.08.2026:
Telegram Stars не требуют юрлица, оплата идёт внутри Telegram, внешнего
платёжного аккаунта и лишнего секрета в `.env` не появляется. Цена в одну
звезду — это меньше цента; она не про деньги, а про то, чтобы подписка была
осознанным действием, а не случайным нажатием: бесплатная кнопка «следить»
набирает подписчиков, которым уведомления не нужны, и они уходят в блок.

Что проверено живым вызовом 01.09.2026, а не взято из памяти:

| Проверка | Ответ Telegram |
|---|---|
| 1 звезда, период 2592000 | ссылка создана |
| 0 звёзд | `Bad Request: total price must be positive` |
| период 86400 (сутки) | `Bad Request: SUBSCRIPTION_PERIOD_INVALID` |

То есть период ровно 30 суток — не наш выбор, а единственное, что принимается.

Три места, где ошибиться дорого.

**`pre_checkout_query` обязан получить ответ за 10 секунд**, иначе Telegram
отменяет платёж. Поэтому здесь только разбор строки и одна проверка владельца
паспорта — ни поиска, ни модели, ни сети.

**Отказывать можно только ДО списания.** `pre_checkout` — последняя точка, где
отказ ничего не стоит клиенту. После `successful_payment` деньги уже сняты, и
любая наша проблема решается на нашей стороне, а не отказом в услуге.

**Апдейт о платеже приходит повторно**, если бот не ответил вовремя. Поэтому
зачисление идемпотентно по `telegram_payment_charge_id`, и держится это
уникальным индексом, а не проверкой «а нет ли уже такого» (см.
`db/repositories/delivery.py::pay_and_activate`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from sniffer.domain.records import Payment

log = structlog.get_logger(__name__)

# Единственный принимаемый период (проверено вызовом). Не константа удобства —
# ограничение Telegram.
SUBSCRIPTION_PERIOD_S = 2_592_000
SUBSCRIPTION_CURRENCY = "XTR"
SUBSCRIPTION_STARS = 1
# Префикс полезной нагрузки счёта. По ней и только по ней восстанавливается,
# за какую именно тему заплатили: 128 байт, в них влезает корень цепочки.
PAYLOAD_PREFIX = "sub"

TITLE = "Слежу за новыми"
DESCRIPTION = (
    "Буду присылать новые объявления по этому запросу по мере их появления. "
    "Одна звезда в месяц, отменяется в любой момент в Telegram."
)
LABEL = "месяц слежения"

OFFER = (
    "Если из этого ничего не подошло — могу следить дальше и присылать новые "
    "объявления по мере появления. Одна звезда в месяц, отмена в любой момент."
)
THANKS = (
    "Подписка включена: слежу за новыми объявлениями по этому запросу. "
    "Присылать буду не больше {max_per_day} в сутки, чтобы не надоесть."
)
ALREADY = "Уже слежу за этим запросом — подписка активна до {until}."
# Отказ в `pre_checkout`: деньги ещё не сняты, и это единственный момент,
# когда отказать не стыдно.
PAYLOAD_REFUSED = "Счёт устарел или относится к другому запросу. Начните заново."
# А это уже после списания: молчать нельзя, врать «всё хорошо» — тем более.
PAYMENT_STRANDED = (
    "Оплата прошла, но я не понял, к какому запросу её отнести. Напишите владельцу — вернём звезду."
)


@dataclass(frozen=True, slots=True)
class Purchase:
    """Разобранный факт оплаты. Всё, что нужно, чтобы включить подписку."""

    passport_root: int
    payment: Payment
    until: datetime


def payload_for(passport_root: int) -> str:
    """Полезная нагрузка счёта. Короткая: в неё влезает 128 байт."""
    return f"{PAYLOAD_PREFIX}:{passport_root}"


def passport_root_from(payload: str) -> int | None:
    """Корень цепочки из нагрузки счёта либо `None`, если это не наш счёт.

    Чужая или испорченная строка — не повод падать: в `pre_checkout` это просто
    вежливый отказ, а падение там означало бы неотвеченный запрос и сорванный
    платёж.
    """
    prefix, _, rest = payload.partition(":")
    if prefix != PAYLOAD_PREFIX or not rest.isdigit():
        return None
    return int(rest)


def purchase_from(
    *,
    user_id: int,
    payload: str,
    charge_id: str,
    amount: int,
    expiration: int | None,
    is_recurring: bool,
    now: datetime | None = None,
) -> Purchase | None:
    """Оплата → готовая к зачислению покупка. `None` — счёт не наш.

    Срок берём у Telegram (`subscription_expiration_date`), а не считаем сами:
    продлевает подписку он, и его дата — единственная правильная. Своя
    арифметика разошлась бы с ней на любой задержке платежа, и подписка
    выключалась бы раньше оплаченного.
    """
    passport_root = passport_root_from(payload)
    if passport_root is None:
        return None
    moment = now or datetime.now(UTC)
    until = (
        datetime.fromtimestamp(expiration, tz=UTC)
        if expiration
        else moment + timedelta(seconds=SUBSCRIPTION_PERIOD_S)
    )
    return Purchase(
        passport_root=passport_root,
        until=until,
        payment=Payment(
            user_id=user_id,
            amount=amount,
            external_id=charge_id,
            is_recurring=is_recurring,
        ),
    )
