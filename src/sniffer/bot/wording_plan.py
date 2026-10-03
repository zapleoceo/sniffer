"""Что бот говорит про лимит и подписку: остаток, исчерпание, `/plan`.

Отдельным модулем от `wording.py`: тот отвечает за слова про выдачу («Нашёл N»,
«ничего не нашлось»), а здесь — про деньги. Меняются они по разным причинам и по
решению разных людей: формулировки paywall утверждает владелец, а заголовок
выдачи правят вместе с ранжированием. Числа берутся из `domain/plans` и нигде не
повторяются словами: «10» в приветствии и «10» в проверке лимита разошлись бы при
первой же правке константы.

Тон — на «вы», спокойно, без таймеров, обратного отсчёта и «последнего шанса»
(R4 §6). Даты всегда конкретные («17 ноября») и по вьетнамскому календарю: слова
«через 28 дней» врут, когда сообщение читают через неделю.

Лист по импортам: только `domain` и `naming`, как `wording.py` и по той же причине —
формулировку проверяют без базы и Telegram.
"""

from __future__ import annotations

from datetime import datetime

from sniffer.bot.naming import plural
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD, SUBSCRIPTION_STARS
from sniffer.domain.quota import Standing
from sniffer.domain.quota_period import VIETNAM

# Родительный падеж: «17 ноября», а не «17 ноябрь». Год не нужен: период кончается
# не позже чем через месяц.
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

QUOTA_UNAVAILABLE = "Не смог проверить остаток карточек. Попробуйте ещё раз через пару минут."
# TODO(A5, платёжный пакет): когда появится `/subscription`, кнопка «Подписка» ведёт
# туда, а этот ответ уходит. Пока оформления нет, говорим об этом прямо: кнопка,
# которая молчит или обещает невозможное, хуже честного «пока не открыто».
SUBSCRIPTION_SOON = (
    "Оформление подписки пока не открыто. Остаток карточек и дату обновления показывает /plan."
)
UNKNOWN_COMMAND = (
    "Не знаю такой команды. Напишите словами, что ищете, или откройте /requests, /new, /plan."
)
SUBSCRIBE_LABEL = f"Подписка — {SUBSCRIPTION_STARS} ⭐/мес"

_FOUND = ("подходящий вариант", "подходящих варианта", "подходящих вариантов")


def ru_date(moment: datetime) -> str:
    """«17 ноября» — число по вьетнамскому календарю, как человек видит его у себя."""
    local = moment.astimezone(VIETNAM)
    return f"{local.day} {_MONTHS[local.month - 1]}"


def balance_line(
    limit: int | None, remaining: int | None, period_end: datetime | None
) -> str | None:
    """Строка остатка над выдачей. `None` — у владельца и слежения её нет."""
    if limit is None or remaining is None or period_end is None:
        return None
    when = ru_date(period_end)
    if limit == FREE_CARDS_PER_PERIOD:
        return f"Бесплатно осталось {remaining} из {limit} до {when}."
    return f"Осталось {remaining} из {limit} до {when}."


def _when(renews: datetime | None) -> str:
    return ru_date(renews) if renews is not None else "в начале следующего периода"


def more_line(count: int, *, limit: int | None, renews: datetime | None) -> str:
    """Честное число того, что лимит не пустил: «ещё 33 подходящих варианта — по подписке».

    У подписчика на потолке 300 подписка ничего не добавит: второй слот прибавляет
    слежение, а не карточки. Предлагать её здесь значило бы обманывать, поэтому ему
    говорят про обновление лимита, а не про оплату.
    """
    found = f"Ещё {count} {plural(count, _FOUND)}"
    if limit == FREE_CARDS_PER_PERIOD:
        return f"{found} — по подписке {SUBSCRIPTION_STARS} ⭐/мес."
    return f"{found} — после обновления лимита {_when(renews)}."


def exhausted_offer(*, total: int | None, renews: datetime | None) -> str:
    """Одно сообщение-предложение: что кончилось, что даёт подписка, чем это не давит.

    Две равноправные дороги и конкретная дата. `total` — сколько подходит, это число
    бесплатно: его показываем и без лимита (так человек видит, что бот не молчит).
    """
    when = _when(renews)
    lines = [f"Бесплатные {FREE_CARDS_PER_PERIOD} карточек закончились — новые откроются {when}."]
    if total is not None:
        lines.append(f"Сейчас подходящих объявлений: {total} — это число я показываю и без лимита.")
    lines += [
        "",
        "Что можно сделать сейчас",
        f"• Оформить подписку: {SUBSCRIPTION_STARS} ⭐ в месяц — до {PAID_CARDS_PER_PERIOD} "
        "карточек за период и слежение за одним поиском, новое придёт сразу.",
        f"• Или подождать до {when}: ваши поиски сохранены, список — /requests.",
    ]
    return "\n".join(lines)


def exhausted_short(*, total: int, renews: datetime | None) -> str:
    """Тот же факт без кнопки оплаты: предложение уже было, второй раз оно давление."""
    when = _when(renews)
    return (
        f"Бесплатные {FREE_CARDS_PER_PERIOD} карточек на этот период закончились — "
        f"новые откроются {when}. Подходящих объявлений сейчас: {total}. "
        "Ваши поиски сохранены: /requests."
    )


def exhausted_cap(*, total: int, renews: datetime | None) -> str:
    """Подписчик упёрся в потолок периода: только информация, без продажи.

    Потолок один на аккаунт и второй подпиской не расширяется — предложить её здесь
    было бы обманом (R2 §3.5, п. 3).
    """
    return (
        f"Лимит карточек на этот период исчерпан — {PAID_CARDS_PER_PERIOD}. "
        f"Новые откроются {_when(renews)}. Подходящих объявлений сейчас: {total}. "
        "Ваши поиски сохранены: /requests."
    )


def plan_text(standing: Standing) -> str:
    """`/plan`: сколько занято, какой потолок, когда обновится. Только чтение."""
    if standing.limit is None:
        return "Для вас лимита карточек нет."
    subscribed = standing.limit != FREE_CARDS_PER_PERIOD
    if standing.period_end is None:
        return (
            f"Бесплатно — {standing.limit} карточек за период. Он начнётся с первой выданной "
            f"карточки: пока использовано 0.\n{_more_by_subscription()}"
        )
    used = (
        f"использовано {standing.used} из {standing.limit}"
        if standing.used <= standing.limit
        else f"использовано {standing.used}, потолок сейчас {standing.limit}"
    )
    when = ru_date(standing.period_end)
    head = "По подписке" if subscribed else "Бесплатно"
    text = f"{head}: {used} карточек.\nОбновится {when}."
    return text if subscribed else f"{text}\n{_more_by_subscription()}"


def _more_by_subscription() -> str:
    return (
        f"Больше — по подписке {SUBSCRIPTION_STARS} ⭐ в месяц: до {PAID_CARDS_PER_PERIOD} "
        "карточек за период и слежение за новыми объявлениями."
    )
