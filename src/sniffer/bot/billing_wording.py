"""Что бот говорит человеку про подписку и оплату: кнопки, условия, счёт, отказы, ответы владельцу.

Здесь только слова и то, что нужно, чтобы собрать их из тарифа. Когда их говорить и
что делать после — решает сервис оплаты; этот файл ничего не решает и никуда не ходит.
Лист по импортам: только `domain` (тарифы, причины отказа). Это закреплено тестом
`tests/test_billing_isolation.py`, а не словами в этой строке.

Число звёзд, период и потолки берутся из `domain/plans.py` и больше нигде не пишутся
буквами: рассинхрон кнопки и счёта — тёмный паттерн, который проект себе запрещает.
Служебные тексты для владельца вынесены в `billing_owner_wording.py`: у них другой адресат.
"""

from __future__ import annotations

from datetime import datetime

from sniffer.domain import plans
from sniffer.domain.billing import Reason
from sniffer.domain.quota_period import VIETNAM

# Версия условий — это версия ТЕКСТА `terms()`: клиент соглашается с конкретным
# текстом, и правка текста без новой версии подменила бы то, с чем он согласился.
# Держит это `tests/test_billing_wording.py`: меняется текст — меняется версия.
TERMS_VERSION = "2026-10-03"
TERMS_DOC = "terms"

_MONTHS = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
)

# Цена стоит на самой кнопке: кнопка «следить», ведущая к счёту без предупреждения о
# деньгах, — тёмный паттерн, даже если речь про звёзды.
SUBSCRIBE_LABEL = f"🔔 Следить за новыми — {plans.SUBSCRIPTION_STARS} ⭐/мес"
ACCEPT_LABEL = "Принимаю условия, перейти к оплате"
TERMS_LABEL = "Читать условия"
CANCEL_LABEL = "Не сейчас"
PAY_LABEL = f"Оформить: {plans.SUBSCRIPTION_STARS} ⭐ в месяц"
INVOICE_LABEL = "Месяц слежения"

OFFER = (
    "Если из этого ничего не подошло — могу следить дальше и присылать новые "
    "объявления по мере появления. "
    f"Подписка: {plans.SUBSCRIPTION_STARS} ⭐ в месяц, отмена в любой момент."
)

ALREADY_ISSUED = "Ссылка на оплату уже выдана — она выше."
STALE_BUTTON = "Эта кнопка устарела. Откройте /subscription заново."
BILLING_OFF = "Оформление подписки пока недоступно. Загляните позже."
UNAVAILABLE = "Сервис временно недоступен. Повторите через минуту."
CANCELLED = "Хорошо, подписку не оформляю. Передумаете — /subscription."
TERMS_CHANGED = "Условия обновились — проверьте их и подтвердите заново."

# Меню команд (`setMyCommands`): описания до 256 символов, команды строчными.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Начать"),
    ("new", "Новый поиск"),
    ("requests", "Мои поиски"),
    ("subscription", "Подписка: оформить или добавить слот"),
    ("terms", "Условия подписки"),
    ("support", "Вопрос владельцу"),
    ("paysupport", "Вопрос по оплате или возврат"),
)


def date_ru(moment: datetime) -> str:
    """«2 ноября 2026» по вьетнамскому календарю: клиенты живут во Вьетнаме."""
    local = moment.astimezone(VIETNAM)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 час», «2 часа», «5 часов»: слово по числу, а не «час(а)»."""
    last_two, last = count % 100, count % 10
    if last_two in range(11, 15) or last == 0 or last >= 5:
        word = many
    else:
        word = one if last == 1 else few
    return f"{count} {word}"


def hours(count: int) -> str:
    return plural(count, "час", "часа", "часов")


def invoice_title(number: int) -> str:
    """«Слежение №2»: в клиенте Telegram у каждой подписки своя строка, различают их по названию."""
    return f"Слежение №{number}"


def invoice_description() -> str:
    stars = plans.SUBSCRIPTION_STARS
    cap = plans.PAID_CARDS_CAP
    return (
        f"Один слот слежения за новыми объявлениями и безлимит карточек (до {cap} за период). "
        f"{stars} ⭐ в месяц, продлевается автоматически, отмена в любой момент в настройках "
        "Telegram. Условия: /terms, вопросы по оплате: /paysupport."
    )


def confirmation(live: int) -> str:
    """Экран с цифрами ДО ссылки: вторую подписку случайно не покупают."""
    stars = plans.SUBSCRIPTION_STARS
    cap = plans.PAID_CARDS_CAP
    consent = (
        f"Нажимая «{ACCEPT_LABEL}», вы подтверждаете, что прочитали условия (/terms) "
        "и согласны с ними."
    )
    if live == 0:
        return (
            f"Подписка: {stars} ⭐ в месяц, продлевается автоматически, отменить можно в любой "
            "момент в настройках Telegram.\n\n"
            f"Что входит: безлимит карточек (до {cap} за период) и один слот слежения за новыми "
            "объявлениями. Платить можно только звёздами внутри Telegram.\n\n" + consent
        )
    have = plural(live, "подписка", "подписки", "подписок")
    slots = plural(live, "слот", "слота", "слотов")
    return (
        f"У вас сейчас {have} ({slots} слежения). Добавить ещё один слот: {stars} ⭐ в месяц, "
        "продлевается автоматически, отменить можно в любой момент. "
        f"Лимит карточек ({cap}) не изменится.\n\n" + consent
    )


def link_ready(number: int) -> str:
    stars = plans.SUBSCRIPTION_STARS
    return (
        f"Ссылка на оплату готова: «{invoice_title(number)}», {stars} ⭐ в месяц. Откройте её "
        "кнопкой ниже и подтвердите оплату в Telegram: пока не подтвердили, звёзды не списываются."
    )


def thanks_first(until: datetime) -> str:
    return (
        f"Оплата получена, спасибо! Подписка действует до {date_ru(until)} и продлится сама. "
        "Отменить можно в любой момент в настройках Telegram (раздел «Мои звёзды», подписки). "
        "Вопросы по оплате: /paysupport."
    )


def thanks_renewal(until: datetime) -> str:
    """Продление отвечает иначе, чем первая оплата: тот же ответ выглядел бы как новое списание."""
    return f"Подписка продлена до {date_ru(until)}. Вопросы по оплате: /paysupport."


# Что Telegram покажет клиенту прямо в окне оплаты: звёзды ещё не списаны, и это
# единственный момент, когда отказ ничего ему не стоит.
_REFUSALS: dict[Reason, str] = {
    Reason.LEGACY_INVOICE: "Этот счёт устарел. Оформите подписку заново командой /subscription.",
    Reason.BAD_PAYLOAD: "Этот счёт устарел. Оформите подписку заново командой /subscription.",
    Reason.FOREIGN_BUYER: "Этот счёт выписан для другого аккаунта. Свою подписку: /subscription.",
    Reason.WRONG_CURRENCY: "Цена изменилась или счёт устарел. Оформите подписку: /subscription.",
    Reason.WRONG_AMOUNT: "Цена изменилась или счёт устарел. Оформите подписку: /subscription.",
    Reason.NOT_A_SUBSCRIPTION: "Этот счёт не подходит. Оформите подписку: /subscription.",
    Reason.NO_CONSENT: "Сначала подтвердите условия: /subscription.",
    Reason.UNAVAILABLE: "Сервис временно недоступен. Повторите через минуту: звёзды не списаны.",
    Reason.TIMEOUT: "Не успел проверить счёт. Повторите через минуту: звёзды не списаны.",
    Reason.INTERRUPTED: "Бот перезапускается. Повторите через минуту: звёзды не списаны.",
}


def refusal(reason: Reason) -> str:
    return _REFUSALS[reason]


def terms(reply_hours: int) -> str:
    """Условия подписки. Меняется текст — меняется `TERMS_VERSION`."""
    stars = plans.SUBSCRIPTION_STARS
    cap = plans.PAID_CARDS_CAP
    free = plans.FREE_CARDS_PER_PERIOD
    return (
        f"<b>Условия подписки</b> (версия {TERMS_VERSION})\n\n"
        f"<b>Что продаётся.</b> Подписка на бота: безлимит карточек (до {cap} за период) и один "
        "слот слежения за новыми объявлениями. Каждая дополнительная подписка добавляет ещё один "
        f"слот; лимит карточек остаётся {cap} на аккаунт. Без подписки — {free} карточек за "
        "период.\n\n"
        f"<b>Цена и период.</b> {stars} ⭐ в месяц (30 суток). Оплата только звёздами Telegram "
        "внутри Telegram.\n\n"
        "<b>Автопродление и отмена.</b> Подписка продлевается сама. Отменить можно в любой момент "
        "в настройках Telegram (раздел «Мои звёзды», подписки). После отмены доступ и слот "
        "сохраняются до конца оплаченного периода.\n\n"
        "<b>Слоты и паузы.</b> Если подписка закончилась, слежения не удаляются, а ставятся на "
        "паузу и возобновляются при продлении.\n\n"
        "<b>Возвраты.</b> Звёзды возвращаются полностью, если услуга не оказана: сбой, двойной "
        "платёж или неверная сумма. Остальные случаи разбираются по обращению в /paysupport в "
        f"течение {hours(reply_hours)}.\n\n"
        "<b>Связь.</b> Вопросы по оплате: /paysupport, остальное: /support. Поддержка Telegram и "
        "@botsupport по покупкам в этом боте помочь не смогут: все вопросы по оплате решает "
        "владелец бота.\n\n"
        "Оформить подписку: /subscription"
    )


def paysupport_prompt(reply_hours: int) -> str:
    return (
        "Вопрос по оплате или возврат: отправьте /paysupport и в том же сообщении напишите, что "
        "произошло и когда — например: /paysupport оплатил, а подписка не появилась. Передам "
        f"владельцу, ответ в течение {hours(reply_hours)}. Поддержка Telegram и @botsupport по "
        "покупкам в этом боте помочь не смогут: все вопросы по оплате решаем мы."
    )


def support_prompt(reply_hours: int) -> str:
    return (
        "Нужна помощь: отправьте /support и в том же сообщении опишите вопрос — например: "
        f"/support как отменить подписку. Передам владельцу, ответ в течение {hours(reply_hours)}. "
        "Поддержка Telegram и @botsupport по покупкам в этом боте помочь не смогут."
    )


def support_sent(reply_hours: int) -> str:
    return f"Передал владельцу. Ответ в течение {hours(reply_hours)}."


SUPPORT_THROTTLED = (
    "Вы уже писали недавно: вопрос передан, ответ придёт в срок. Если нужно добавить деталей — "
    "напишите ещё раз через час."
)
SUPPORT_UNAVAILABLE = "Сейчас не получается передать вопрос. Попробуйте через несколько минут."

_WHY: dict[Reason, str] = {
    Reason.LEGACY_INVOICE: "счёт устарел",
    Reason.BAD_PAYLOAD: "счёт устарел",
    Reason.FOREIGN_BUYER: "счёт выписан на другой аккаунт",
    Reason.WRONG_CURRENCY: "сумма не совпала с ценой",
    Reason.WRONG_AMOUNT: "сумма не совпала с ценой",
    Reason.NOT_A_SUBSCRIPTION: "Telegram не оформил подписку",
}


def why(reason: Reason) -> str:
    return _WHY.get(reason, "платёж не опознан")


def payment_refunded(reason: Reason) -> str:
    return (
        f"Оплата не подошла к нашему счёту ({why(reason)}), поэтому звёзды возвращены полностью. "
        "Оформить подписку заново: /subscription."
    )


def payment_refund_failed(reply_hours: int) -> str:
    return (
        "Оплата получена, но не подошла к нашему счёту, а вернуть звёзды автоматически не "
        f"получилось. Владелец уже уведомлён и вернёт их вручную в течение {hours(reply_hours)}. "
        "Вопросы: /paysupport."
    )


def payment_unrecorded(reply_hours: int) -> str:
    return (
        "Оплата получена, но записать её сейчас не удалось. Владелец уже уведомлён; если подписка "
        f"не появится в течение {hours(reply_hours)}, напишите /paysupport."
    )


def subscription_refunded() -> str:
    return "Платёж возвращён. Если это неожиданно — напишите /paysupport."
