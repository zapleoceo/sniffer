"""Что бот говорит человеку: заголовок выдачи, отказы, подсказки, «Понял: …».

Здесь только слова и то, что нужно, чтобы собрать их из паспорта. Когда их
говорить и что делать после — решает `conversation.py`; этот файл ничего не
решает и никуда не ходит.

Отдельным модулем, потому что в `conversation.py` это была вторая
ответственность: ход диалога (кто за кем идёт, что журналировать, где закончить
ход) и формулировки для клиента меняются по разным причинам и разными людьми.
Правка подсказки «как сузить» не должна лежать в одном файле с порядком шагов
поиска, а тест формулировки — требовать для себя ни базы, ни Telegram.

Лист по импортам: только `domain` и словарь поиска. Это закреплено тестом
`tests/test_wording_isolation.py`, а не словами в этой строке.
"""

from __future__ import annotations

from sniffer.bot.naming import budget_phrase, category_noun
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.plans import (
    FREE_CARDS_PER_PERIOD,
    PAID_CARDS_PER_PERIOD,
    PAID_SEARCHES,
    SUBSCRIPTION_STARS,
)
from sniffer.search.vocabulary import city_name, served_cities

NOTHING_FOUND = (
    "По этому запросу ничего не нашлось. Попробуйте иначе: без марки, "
    "с другим бюджетом или другой формулировкой."
)
SEARCH_FAILED = "Не смог доискать: источники не ответили. Попробуйте ещё раз через пару минут."
NO_REQUEST_YET = "Сначала напишите, что ищете, — а потом уточним."
NOTHING_TO_REFINE = "Уточнить больше нечего. Переформулируйте запрос, и поищу заново."
UNSERVED_CITY = (
    "{city} я пока не ищу: реестр чатов и параметры досок собраны под другие города — "
    "{served}. Напишите запрос по одному из них, и поищу."
)


# Приветствие `/start`. Числа берутся из `domain/plans`, а не пишутся словами: «10» здесь
# и «10» в проверке лимита обязаны быть одним числом. Текст — ПРЕДЛОЖЕНИЕ, финальную
# редакцию утверждает владелец; потому это константа, которую можно править одной
# строкой, ничего не трогая вокруг. Человек видит одно слово — «поиск», а не «запрос»
# и «ветка» (tests/test_thread_menu.py сторожит это слово).
# Постоянная клавиатура под полем ввода: навигация кнопками, а не командами (решение владельца
# 04.10.2026). Каждая кнопка вызывает то же действие, что команда рядом; тексты живут здесь, а
# перехват — в `handlers/menu.py` раньше общего текстового обработчика, иначе подпись кнопки
# ушла бы в поиск как поисковая фраза.
BTN_NEW = "🔍 Новый поиск"
BTN_REQUESTS = "📋 Мои поиски"
BTN_PLAN = "📊 Тариф и остаток"
BTN_SUBSCRIPTION = "⭐ Подписка"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = (BTN_NEW, BTN_REQUESTS, BTN_PLAN, BTN_SUBSCRIPTION, BTN_HELP)
HELP = (
    "Что умею: ищу частные объявления по чатам и доскам Вьетнама и приношу ссылки на оригиналы.\n\n"
    "• 🔍 Новый поиск — напишите или наговорите, что нужно.\n"
    "• 📋 Мои поиски — ваши поиски; /watch — слежение за новыми, слоты и фильтр.\n"
    "• 📊 Тариф и остаток — сколько карточек осталось в периоде.\n"
    "• ⭐ Подписка — безлимит карточек и слежение за новыми.\n\n"
    "Вопрос владельцу: /support. Условия подписки: /terms."
)

GREETING = (
    "Я ищу частные объявления по чатам и доскам Вьетнама и приношу ссылки на оригиналы.\n\n"
    "Напишите словами, что нужно: <i>ищу скутер в Нячанге до 400 долларов</i> "
    "или <i>сниму квартиру в Нячанге до 10 млн донгов</i>.\n\n"
    "Скажу, сколько вариантов подходит, и, если их много, уточню парой вопросов — "
    "отвечать можно кнопкой или словами. Считать и уточнять бесплатно. Объявление не "
    "перепечатываю: даю ссылку на источник и честно помечаю, "
    "если лот старый и мог быть продан.\n\n"
    f"Бесплатно — {FREE_CARDS_PER_PERIOD} карточек на ваш Telegram-аккаунт за период: он "
    f"начнётся с первой выданной карточки. Поэтому лучше сразу сузить поиск — бюджет, район, "
    f"марка: тогда эти {FREE_CARDS_PER_PERIOD} будут самыми подходящими, а не просто самыми "
    "свежими. Карточка, которую вы уже видели в этом периоде, повторно не считается.\n"
    f"Больше — подписка {SUBSCRIPTION_STARS} ⭐ в месяц: до {PAID_CARDS_PER_PERIOD} карточек за "
    "период и слежение за новыми объявлениями.\n\n"
    "Бесплатно — один поиск за раз: допишите марку, район или бюджет, и он уточнится. Чтобы "
    "искать другое, напишите /new: текущий поиск заменится новым, либо удалите его в «Мои "
    f"слежения». С подпиской — до {PAID_SEARCHES} поисков одновременно. "
    "Последние поиски — /requests, остаток карточек и дата обновления — /plan."
)

# Как назвать категорию во множественном числе в заголовке выдачи. Русские слова,
# потому что заголовок читает человек, а не `passport.category.value` («motorbike»).
_CATEGORY_PLURAL: dict[Category, str] = {
    Category.MOTORBIKE: "байков",
    Category.APARTMENT: "квартир",
    Category.ROOM: "комнат",
    Category.HOUSE: "домов",
    Category.CAR: "машин",
    Category.BICYCLE: "велосипедов",
}


def _is_broad(passport: Passport) -> bool:
    """Запрос без единого сужающего факта — только категория (и, может, город).

    «Скутер» без бюджета, марки, модели и объёма — это ещё не запрос, а тема:
    под неё подходит пол-базы. Показать пять свежих и молча назвать это ответом —
    ровно та жалоба владельца «находит шлак в большом количестве, не уточняя».
    Такой выдаче нужен честный заголовок и приглашение сузить.
    """
    a = passport.attributes
    return not (a.get("model") or a.get("brand") or a.get("engine_cc") or passport.budget.max)


def result_header(
    passport: Passport, total: int, shown: int, *, capped: bool = False, unpriced: int = 0
) -> str:
    """Строка над карточками: что нашлось и, если запрос широкий, как сузить.

    Объяснение — не вежливость, а ответ на «не объясняя»: пять карточек без
    контекста не говорят, из скольких они выбраны и почему именно эти. Широкий
    запрос вдобавок сам просит сузить — но не вопросом до выдачи (это была бы
    прежняя форма, которую владелец отверг), а приглашением поверх уже показанных
    результатов: search-first остаётся.
    """
    # Источник отдал ровно потолок своей выборки: настоящее число больше, и «нашёл 100» было бы
    # ложью с круглой цифрой. Говорим «не меньше» — честнее, чем недосчитанное точное число.
    count = f"не меньше {total}" if capped else str(total)
    if total <= shown:
        return "Вот что нашлось:" if total > 1 else "Нашёлся один вариант:"
    # Бюджет назван, а у части лотов цены нет: они не отсеяны, но и не подтверждены — честно
    # называем, сколько их, а в самой выдаче они стоят ниже лотов с ценой.
    if unpriced and passport.budget.max:
        count = f"{count}, без цены — {unpriced}"
    if _is_broad(passport):
        noun = _CATEGORY_PLURAL.get(passport.category) if passport.category else None
        many = f"{noun} нашлось много" if noun else "нашлось много"
        return (
            f"Запрос широкий — {many} ({count}). Показываю {shown} самых свежих.\n"
            f"{_narrowing_advice(passport)}"
        )
    return f"Подходит {count}, показываю {shown} лучших:"


def _narrowing_advice(passport: Passport) -> str:
    """Подсказка использует свойства предмета, который действительно ищут."""
    if passport.category in {Category.APARTMENT, Category.ROOM, Category.HOUSE}:
        return (
            "Чтобы сузить, допишите бюджет, район или важные условия — "
            "например «2 спальни с мебелью до 10 млн»."
        )
    if passport.category in {Category.MOTORBIKE, Category.CAR, Category.BICYCLE}:
        return (
            "Чтобы сузить, допишите бюджет, марку или модель — например «yamaha до 500» "
            "или «honda lead»."
        )
    return "Чтобы сузить, допишите бюджет, район или обязательные условия."


def nothing_found(passport: Passport) -> str:
    """Пустая выдача предлагает ослабить только уместные для категории критерии."""
    if passport.category in {Category.APARTMENT, Category.ROOM, Category.HOUSE}:
        return (
            "По этому запросу ничего не нашлось. Попробуйте другой бюджет, район, "
            "количество комнат или условия."
        )
    if passport.category in {Category.MOTORBIKE, Category.CAR, Category.BICYCLE}:
        return NOTHING_FOUND
    return (
        "По этому запросу ничего не нашлось. Попробуйте изменить бюджет, место "
        "или обязательные условия."
    )


def unserved(city: str | None) -> str:
    """Список городов берётся из словаря: набранный руками, он разъедется первым."""
    return UNSERVED_CITY.format(
        city=city_name(city, "ru") or "Этот город", served=", ".join(served_cities("ru"))
    )


def accepted(passport: Passport) -> str:
    """Показываем, что поняли, — это дешевле лишнего уточняющего вопроса."""
    parts: list[str] = []
    category = category_noun(passport)
    if category:
        parts.append(category)
    for key in ("brand", "model"):
        if passport.attributes.get(key):
            parts.append(str(passport.attributes[key]))
    city = city_name(passport.city, "ru")
    if city:
        parts.append(city)
    budget = budget_phrase(passport)
    if budget:
        parts.append(budget)
    transmission = passport.attributes.get("transmission")
    if transmission:
        parts.append(
            {"automatic": "автомат", "manual": "механика", "semi": "полуавтомат"}.get(
                str(transmission), str(transmission)
            )
        )
    engine_cc = passport.attributes.get("engine_cc")
    if engine_cc is not None:
        direction = passport.attributes.get("engine_cc_dir")
        prefix = {"min": "от ", "max": "до "}.get(str(direction), "")
        parts.append(f"{prefix}{engine_cc} cc")
    rooms = passport.attributes.get("rooms")
    if rooms is not None:
        parts.append(f"{rooms} комн.")
    furnished = passport.attributes.get("furnished")
    if furnished is True:
        parts.append("с мебелью")
    elif furnished is False:
        parts.append("без мебели")
    understood = ", ".join(parts) if parts else "запрос как есть"
    action = "Ищу подходящие предложения"
    if passport.intent is Intent.RENT:
        action = "Ищу варианты аренды"
    elif passport.intent is Intent.SELL:
        action = "Ищу покупателей"
    elif passport.intent is Intent.RENT_OUT:
        action = "Ищу арендаторов"
    return f"Понял: {understood}. {action}, это занимает до минуты."
