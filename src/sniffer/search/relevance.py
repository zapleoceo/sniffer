"""Детерминированный отбор находок по паспорту до показа карточек."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from math import exp

from sniffer.domain.fingerprint import normalized
from sniffer.domain.passport import Budget, Currency, Intent, Passport
from sniffer.search.engine_size import listing_cc_values
from sniffer.search.intake_rules import category_of, detect_brand, detect_transmission
from sniffer.search.market_terms import RENTAL_PRICE_MARKERS, RENTAL_STEMS
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.search.vocabulary import attribute_phrases, models_named_in
from sniffer.sources.base import RawItem

PRICE_OVER_BUDGET = 1.30

# Полоса допуска объёма, когда клиент назвал точку, а не границу: «200 кубиков»
# — это про класс мотоцикла, 175 и 250 клиент назовёт тем же поиском, 700 — нет.
ENGINE_BAND = 0.25

# Возраст, после которого лот уходит из живой выдачи. Это не тот порог, что в
# `verifier/liveness` (14 дней): там решается, когда ПРЕДУПРЕДИТЬ, и карточка
# честно говорит «объявлению N дней, могло быть продано» — лот старше двух
# недель чаще продан, чем нет, но всё ещё бывает жив. Здесь решается другое: с
# какого возраста показывать лот ВМЕСТО более свежего значит врать. Знание
# разное, поэтому и число своё, а не производное от чужого.
#
# Вдвое дальше порога предупреждения: пометка «могло быть продано» к этому
# моменту верна дольше, чем лот вообще был живым. Такая жёсткость допустима
# ровно потому, что подстрахована — если после отсева не осталось ничего,
# старое показывается всё равно (`rank_items`). Живой повод: 02.09.2026 в
# выдаче всплыл лот 59-дневной давности.
LIVE_MAX_AGE_DAYS = 28


def with_vnd_budget(plan: SearchPlan, passport: Passport, rate: float | None) -> SearchPlan:
    """Передать источникам VND-бюджет, если клиент указал сумму в USD."""
    budget = _budget_vnd(passport.budget, rate)
    if budget is None:
        return plan
    tasks = [_with_budget(task, budget) for task in plan.tasks]
    return plan.model_copy(update={"tasks": tasks})


def rank_items(
    passport: Passport,
    items: list[RawItem],
    *,
    usd_vnd: float | None,
    now: datetime | None = None,
) -> list[RawItem]:
    """Порядок и отсев перед показом.

    Порядок: свежесть, бюджет, совпадение атрибутов, продавец. Явное превышение
    потолка — противоречие запросу и отсекается; неизвестная цена остаётся.

    Отсев появился после жалобы 02.09.2026: на «нужен скутер honda lead» бот
    отдал три Airblade и лот 59-дневной давности. Сортировка одна такую выдачу
    не лечит — она ставит мусор ниже, а показываем мы первые пять, и когда
    лучшего нет, первыми пятью оказывается мусор.

    Отсев в две очереди, и они отменяются по-разному:

    Перед отсевом кросспосты схлопываются (`_dedup`): один лот, переопубликованный
    в несколько чатов с новыми эмодзи и переставленными фразами, — это одна
    карточка, а не пять (spec-v2, 2.7).

    * **противоречие запросу** (`_contradicts`) — чужая категория, чужая модель,
      чужая марка, чужая коробка, объём вне запрошенного диапазона, оффер аренды
      на запрос покупки, цена явно выше потолка. Всё это факты о
      предмете, прочитанные из его собственных слов, а не догадки: клиент назвал
      Lead — Airblade не «похуже», а не тот предмет (spec-v2, 3.2). Очередь НЕ
      отменяется: если подходящего на рынке нет, честнее пустой ответ, который
      прямо советует «попробуйте без марки» (`NOTHING_FOUND`), чем пять чужих
      карточек — ровно то, на что жаловался владелец. Неизвестное свойство
      противоречием не считается: лот, не назвавший марку/коробку, остаётся;
    * **совсем старое** (`_too_old`) — догадка о живости, а не факт о предмете, и
      лот старше порога всё-таки бывает жив. Поэтому очередь отменяется, когда
      после неё пусто: пустая выдача хуже слабой, а карточка сама скажет
      «объявлению N дней, могло быть продано» (`bot/cards.py`).

    Порога по итоговому баллу здесь нет намеренно, хотя в подписке он есть
    (`matching.MATCH_MIN_SCORE`). Там `posted_at` у карточки обязателен, а в
    живом поиске он необязателен, и `_freshness` отдаёт ноль ОДИНАКОВО лоту без
    даты и лоту двухмесячной давности. Балльный порог отсекал бы не старое, а
    неизвестное — то есть законную карточку с честной пометкой «дата публикации
    неизвестна» (spec-v2, 3.3).
    """
    moment = now or datetime.now(UTC)
    ranked = sorted(items, key=lambda item: _score(item, passport, usd_vnd, moment), reverse=True)
    # Дедуп ПОСЛЕ сортировки: у одинакового текста балл одинаков, а из равных
    # первым стоит свежайший — его и оставляем (spec-v2 2.7). До отсева, но это
    # безразлично: у кросспоста текст один, значит и вердикт `_contradicts` один.
    unique = _dedup(ranked)
    asked_for = [item for item in unique if not _contradicts(item, passport, usd_vnd)]
    return [item for item in asked_for if not _too_old(item, moment)] or asked_for


def _contradicts(item: RawItem, passport: Passport, usd_vnd: float | None) -> bool:
    """Жёсткие факты запроса — не пожелания для сортировки.

    Неизвестное свойство пропускаем, явно противоположное — никогда. Для
    конкретной модели требуем её имя: показать CB200X вместо названного Lead
    хуже честной пустой выдачи.
    """
    if _other_category(item, passport) or _other_model(item, passport):
        return True
    # Прокат — оффер аренды, чужая сторона сделки: клиент с intent=BUY хочет
    # купить. Отсекаем только у покупателя — клиенту с intent=RENT прокат нужен.
    if passport.intent is Intent.BUY and _is_rental_offer(item.title, item.text):
        return True
    text = f"{item.title} {item.text}"
    wanted_model = str(passport.attributes.get("model") or "")
    if wanted_model and wanted_model not in models_named_in(passport.category, text):
        return True
    if _contrary_attribute(passport, "brand", text):
        return True
    if _contrary_attribute(passport, "transmission", text):
        return True
    if _wrong_engine(
        text, passport.attributes.get("engine_cc"), passport.attributes.get("engine_cc_dir")
    ):
        return True
    ceiling = _budget_ceiling_vnd(passport.budget, usd_vnd)
    return ceiling is not None and item.price_vnd is not None and item.price_vnd > ceiling


def _contrary_attribute(passport: Passport, field: str, text: str) -> bool:
    """Текст лота ЯВНО называет другое известное значение свойства.

    Марку и коробку лота читаем теми же детекторами, что читают запрос клиента
    (`detect_brand` / `detect_transmission`), а не словарём фраз. Через словарь
    это не работало и молча: слов марки в `ATTRIBUTE_TERMS` нет вовсе (там
    свойства — коробка, документы, состояние), поэтому марка в тексте лота не
    находилась НИКОГДА, и фильтр по марке был мёртв — Honda приходила на «ямаха»
    (realcheck 03.09.2026). Детектор ещё и не зависит от категории: на мутном
    «надёжное ямаха» без категории фразы не разрешались, и фильтр молчал вторично.

    Неизвестность — не противоречие: лот, не назвавший марку/коробку, остаётся
    (та же дисциплина, что у категории и модели). Марку `detect_brand` выводит и
    из модели: «Exciter» — Yamaha, поэтому на запрос honda он отсеётся, даже не
    написав «yamaha» словом.
    """
    wanted = passport.attributes.get(field)
    if not wanted:
        return False
    if field == "brand":
        found = detect_brand(text, passport.category)
    elif field == "transmission":
        found = detect_transmission(text, passport.category)
    else:
        return False
    return found is not None and str(found) != str(wanted)


def _wrong_engine(text: str, wanted: object, direction: object = None) -> bool:
    """Объём лота противоречит запрошенному — с учётом направления.

    Направление несёт паспорт (`engine_cc_dir`): «от 250» отсекает лот с cc<250,
    «до 250» — с cc>250, без направления — полоса ±band вокруг точки. Объёмы лота
    читает `listing_cc_values`, в т.ч. голым числом («nvx 125»): раньше читалось
    только «125cc», и «250 минимум» показывал 124–125cc (живой отказ 02.09.2026).

    `all(...)`, а не `any(...)`: если ХОТЬ ОДИН прочитанный объём в запросе, лот
    остаётся. Неизвестный объём (ни одного числа) противоречием не считается.
    """
    if wanted is None:
        return False
    found = listing_cc_values(text)
    if not found:
        return False
    target = float(str(wanted))
    if direction == "min":
        return all(value < target for value in found)
    if direction == "max":
        return all(value > target for value in found)
    return all(abs(value - target) > target * ENGINE_BAND for value in found)


# ── Прокат: оффер аренды в тексте лота ───────────────────────────────────────
_RENTAL_RE = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(stem).replace(r"\ ", r"\s+") for stems in RENTAL_STEMS.values() for stem in stems
    )
    + r")\w*",
    re.IGNORECASE,
)
# Продажа в заголовке снимает срабатывание проката, отрицание перед словом
# аренды — тоже. «прода\w*» ловит продам/продаю/продажа; «не»/«без» — целым
# словом, иначе «недорогая аренда» приняли бы за отрицание.
_SALE_RE = re.compile(r"\b(?:прода|sell|bán)\w*|\bfor\s+sale\b", re.IGNORECASE)
_NEG_RE = re.compile(r"\b(?:не|без|không|not)\b", re.IGNORECASE)


def _is_rental_offer(title: str, text: str) -> bool:
    """Явное предложение аренды в тексте лота — прокат, а не продажа.

    Клиент с intent=BUY хочет купить; «🏍 Аренда мотоциклов», «‼️АРЕНДА БАЙКОВ
    (сутки/месяц)‼️» — оффер аренды, сторона автора (spec-v2 2.7, замер
    02.09.2026: лезли в топ почти любого покупательского запроса). Ловим по ЯВНОМУ
    предложению, а не по слову «аренда» где попало: ценник за период однозначен
    где угодно, а рамочное слово — только в заголовке и не под продажей/отрицанием,
    иначе «продам, не для аренды» отсеклось бы как прокат.
    """
    if any(marker in f"{title} {text}".casefold() for marker in RENTAL_PRICE_MARKERS):
        return True
    head = normalized(title)
    match = _RENTAL_RE.search(head)
    if match is None or _SALE_RE.search(head):
        return False
    return not _NEG_RE.search(head[: match.start()])


def _dedup(items: list[RawItem]) -> list[RawItem]:
    """Схлопнуть кросспосты, оставив первый — после сортировки это свежайший.

    Один лот приходит в несколько чатов и переопубликуется с новыми эмодзи,
    пробелами, переставленными или задублированными фразами (замер 02.09.2026:
    «Honda Air Blade 2012» и «SYM ATTILAVTS 124» по два раза). Точный хэш такое не
    ловит — эмодзи и повтор фразы дают разный текст. Поэтому отпечаток — МНОЖЕСТВО
    слов заголовка и текста: у переоформленного кросспоста набор слов тот же, а у
    другого лота (иная цена, год, лишняя фраза) — другой, и разные лоты одной
    модели не схлопываются. Плюс дедуп по `(source, external_id)` — тот же лот,
    вынутый источником дважды. Пустой отпечаток не схлопывает: лот без текста не
    дубликат такого же безмолвного.
    """
    seen_ids: set[tuple[str, str]] = set()
    seen_words: set[frozenset[str]] = set()
    kept: list[RawItem] = []
    for item in items:
        ident = (item.source, item.external_id)
        words = frozenset(normalized(f"{item.title} {item.text}").split())
        if ident in seen_ids or (words and words in seen_words):
            continue
        seen_ids.add(ident)
        if words:
            seen_words.add(words)
        kept.append(item)
    return kept


def _other_category(item: RawItem, passport: Passport) -> bool:
    """Лот НЕ того рода, о котором просил клиент: комната в выдаче скутеров.

    Фильтр клиентский по той же причине, что и модельный, только причина здесь
    сильнее: у телеграм-чата структурного поля категории нет вовсе, а ищет он
    словами — «продам», «Нячанг», «срочно» стоят в объявлении о чём угодно.
    Модельный фильтр такой лот не видит: комната модели не называет, и в пятёрку
    она проходит по свежести (замер 02.09.2026 — 38 карточек мимо запроса из 92,
    и почти все из-за этого).

    Категория лота читается ТЕМ ЖЕ разбором, которым читается запрос клиента
    (`intake_rules.category_of`): знание «какими словами называют предмет» одно,
    и второй парсер здесь однажды разошёлся бы с первым. И читается она с тем же
    выводом из модели: «Honda Lead 110 2008» слова «скутер» не содержит, но Lead
    бывает только у мотобайка, и без этого вывода такой лот проходил в выдачу
    студий как «неизвестная категория» (realcheck 03.09.2026).

    Ступень не отменяется при пустом результате, и это не симметрично возрасту.
    Возраст — догадка о живости, чужая категория — факт о предмете, прочитанный
    из его собственных слов. Показать комнату вместо скутера, когда скутеров не
    нашлось, значит вернуть ровно ту жалобу, ради которой отсев и появился:
    пустой ответ прямо советует переформулировать, а комната говорит «бот меня
    не услышал». Подстраховка у ступени своя и внутренняя: лот, чью категорию
    прочитать не удалось, чужим НЕ считается — неизвестность это не
    несовпадение, то же правило, что у модели и прочих атрибутов.
    """
    if passport.category is None:
        return False
    named = category_of(f"{item.title} {item.text}")
    return named is not None and named is not passport.category


def _other_model(item: RawItem, passport: Passport) -> bool:
    """Лот НЕ той модели, которую назвал клиент.

    Фильтр клиентский, потому что структурного поля модели у доски нет: у
    Chotot есть `motorbiketype`, `motorbikecapacity` и `motorbikebrand` — тип,
    объём и марка, — и это всё (spec-v2, 4.1.1). Марки мало: на «honda lead»
    `motorbikebrand=1` отдаёт все 26 Хонд Нячанга, а среди них Airblade, Vision
    и Wave. Именем модели в `q` доски это тоже не решается — `q` складывается с
    фильтрами через И и гасит их в ноль.

    Эта функция различает известную чужую модель и неизвестность. Вызывающий
    применяет более строгий контракт к конкретно названной модели: без её имени
    карточка не доказывает соответствие и не показывается.
    """
    wanted = str(passport.attributes.get("model") or "")
    if not wanted:
        return False
    named = models_named_in(passport.category, f"{item.title} {item.text}")
    return bool(named) and wanted not in named


def _too_old(item: RawItem, now: datetime) -> bool:
    """Лот, который почти наверняка продан.

    Лот без даты старым НЕ считается: про него ничего не известно, а
    неизвестность — не возраст. Именно поэтому отсев идёт по дате, а не по
    итоговому баллу: балл у этих двух случаев одинаковый.
    """
    posted = _posted_utc(item)
    if posted is None:
        return False
    return (now - posted).days > LIVE_MAX_AGE_DAYS


def _with_budget(task: SearchTask, budget: dict[str, float | str]) -> SearchTask:
    return task.model_copy(update={"params": {**task.params, "budget": budget}})


def _budget_vnd(budget: Budget, rate: float | None) -> dict[str, float | str] | None:
    if budget.currency is Currency.VND:
        multiplier = 1.0
    elif budget.currency is Currency.USD and rate is not None:
        multiplier = rate
    else:
        return None
    if budget.min is None and budget.max is None:
        return None
    return {
        "min": (budget.min or 0) * multiplier,
        "max": (budget.max or 10_000_000_000) * multiplier,
        "currency": Currency.VND.value,
        "period": budget.period.value,
    }


def _score(item: RawItem, passport: Passport, usd_vnd: float | None, now: datetime) -> float:
    return (
        0.35 * _freshness(item, now)
        + 0.30 * _price_fit(item, passport.budget, usd_vnd)
        + 0.25 * _attribute_fit(item, passport)
        + 0.10 * 0.5
    )


def _freshness(item: RawItem, now: datetime) -> float:
    posted = _posted_utc(item)
    if posted is None:
        return 0.0
    age_hours = max(0.0, (now - posted).total_seconds() / 3600)
    return exp(-age_hours / 48)


def _posted_utc(item: RawItem) -> datetime | None:
    """Наивную метку источника считаем UTC: иначе вычитание уронит всю выдачу."""
    posted = item.posted_at
    if posted is None:
        return None
    return posted if posted.tzinfo else posted.replace(tzinfo=UTC)


def _price_fit(item: RawItem, budget: Budget, usd_vnd: float | None) -> float:
    ceiling = _budget_ceiling_vnd(budget, usd_vnd)
    if ceiling is None:
        return 0.5
    if item.price_vnd is None:
        return 0.25
    if item.price_vnd <= ceiling:
        return 1.0
    return max(0.0, (PRICE_OVER_BUDGET - item.price_vnd / ceiling) / (PRICE_OVER_BUDGET - 1))


def _budget_ceiling_vnd(budget: Budget, usd_vnd: float | None) -> float | None:
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return budget.max
    if budget.currency is Currency.USD and usd_vnd is not None:
        return budget.max * usd_vnd
    return None


def _attribute_fit(item: RawItem, passport: Passport) -> float:
    if passport.category is None or not passport.attributes:
        return 0.5
    haystack = f"{item.title} {item.text}"
    matched = sum(
        _mentions(passport, field, value, haystack) for field, value in passport.attributes.items()
    )
    # Отсутствующее слово не считаем несовпадением: Chotot уже мог отфильтровать
    # это свойство структурным полем, а оно не обязано повторяться в заголовке.
    return 0.5 + 0.5 * matched / len(passport.attributes)


def _mentions(passport: Passport, field: str, value: object, haystack: str) -> bool:
    """Назван ли этот атрибут в тексте лота.

    Модель — не свойство, а имя, и слов рынка у неё нет. Ищется она тем же
    знанием, которым узнаётся в запросе клиента: иначе «model» попадало бы в
    знаменатель доли совпавших атрибутов, никогда не попадая в числитель, и
    добавление модели в паспорт молча роняло бы балл КАЖДОГО лота.
    """
    if field == "model":
        return str(value) in models_named_in(passport.category, haystack)
    # Марка, как и модель, — имя, а не свойство рынка: слов в `ATTRIBUTE_TERMS`
    # у неё нет, ищется тем же детектором. Иначе «brand» попадала бы в
    # знаменатель доли атрибутов, никогда не попадая в числитель, и роняла бы
    # балл каждого лота.
    if field == "brand":
        return detect_brand(haystack, passport.category) == str(value)
    phrases = [
        phrase
        for lang in ("ru", "vi", "en")
        for phrase in attribute_phrases(passport.category, {field: value}, lang)
    ]
    folded = haystack.casefold()
    return any(phrase.casefold() in folded for phrase in phrases)
