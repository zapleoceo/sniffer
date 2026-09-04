"""Охранник выдачи: последняя проверка карточек перед показом клиенту.

Зачем он нужен, измерено, а не выведено рассуждением. Замер 01.09.2026 на
живых чатах Нячанга: цена в объявлении есть почти всегда, но метку «Цена»
пишет меньшинство. Реально встречается «Штормовая скидка! 40 миллионов»,
«кастомный Honda Hornet 600 за 50.000.000», «52,500,000dong / 2000usd». Наш
регексп (`sources/telegram_mapping.py`) берёт сумму только после явной метки —
и это правильно для регекспа, иначе «125cc» и год выпуска станут донгами. Но
следствие тяжёлое: `price_vnd` пуст у большинства телеграм-находок,
`_price_fit` возвращает им одинаковые 0.25, и **бюджет клиента на телеграм не
влияет вовсе**. Отсюда мотоцикл за 100 000 000 VND в ответ на «до 300 USD».

То же со смыслом: на «сниму квартиру до 10 млн» приезжали часы Casio, ноутбук
Samsung и колонка JBL — доска отдала свою категорию как умела, а отличить
квартиру от колонки регекспом нельзя.

Обе задачи — чтение свободного текста. По решению владельца проверка идёт
через более сильный `chat:sales`, одной пачкой на всю выдачу.

Три правила, без которых охранник вреднее пользы.

**Показываем только то, что написал продавец.** Модель возвращает не только
число, но и `price_text` — точный фрагмент объявления. Мы проверяем, что он
действительно есть в тексте, и лишь тогда используем: число идёт в
ранжирование, фрагмент — в карточку. Не сошлось — цены нет, как и раньше.
Придуманная цена не может стать утверждением, показанным клиенту (spec-v2, 3.1).

**Отказ брокера не отменяет выдачу.** Охранник — улучшение, а не условие
работы: недоступная модель означает выдачу как раньше, а не пустой ответ.

**Пустая выдача — честный ответ.** Если проверка отбраковала всё, клиент видит
«ничего не нашлось». Это правда, и она полезнее пяти чужих карточек.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from sniffer.broker.client import BrokerClient, BrokerError
from sniffer.broker.usage import default_usage_sink
from sniffer.domain.passport import Category, Currency, Intent, Passport

# Словарь рынка — знание о том, как предмет называют люди. Охраннику он
# нужен ровно за тем же, за чем планировщику: говорить с моделью словами
# объявления, а не именами полей.
from sniffer.search.market_terms import CATEGORY_TERMS
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)

# Сколько карточек отдаём на проверку. Показываем клиенту не больше `MAX_CARDS`,
# поэтому проверять всю сотню находок — платить за то, чего никто не увидит.
GUARD_ITEMS = 12
# Объявления бывают на сорок строк. Первых четырёхсот знаков хватает и на
# предмет, и на цену: продавцы пишут то и другое сразу.
GUARD_TEXT_CHARS = 400
# Свой потолок времени. У плана 90 с (spec-v2, 2.3), и охранник не вправе их
# удваивать: лучше показать непроверенное, чем заставить ждать две минуты.
GUARD_TIMEOUT_S = 20.0
GUARD_CAPABILITY = "chat:sales"
# Замер трёх ранее оборванных ответов: 896–960 output tokens на 12 карточек.
# Старые 896 не вмещали why/price_text даже на sales. Запас сохраняет пояснения;
# это потолок, а не обязательный расход. Результаты: docs/json-output-experiments.md.
GUARD_TOKENS_PER_ITEM = 128
GUARD_TOKENS_OVERHEAD = 512
# Верхняя граница правдоподобия: 10 млрд донгов — это 380 тысяч долларов.
# Больше — ошибка чтения, а не цена байка или квартиры в Нячанге.
MAX_PLAUSIBLE_VND = 10_000_000_000
# Насколько дороже бюджета находка ещё имеет право попасть в выдачу. Ровно
# столько же прощает ранжирование (`search/relevance.py`): клиент вправе
# увидеть «чуть дороже» и решить сам.
PRICE_TOLERANCE = 1.30

INTENT_WORDS = {Intent.BUY: "купить", Intent.RENT: "снять"}
# Город в паспорте — ключ, а не название. Модели нужно второе.
CITY_WORDS = {"nha_trang": "Нячанг", "da_nang": "Дананг"}

SYSTEM = (
    "Ты проверяешь объявления перед показом клиенту. Отвечай только JSON по схеме. "
    "Ничего не выдумывай: если цены в тексте нет, оставь price_text пустым."
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Решение по одной карточке."""

    keep: bool
    reason: str = ""
    subject: str = "unknown"
    price_text: str = ""
    price_vnd: int | None = None


def guard_schema() -> dict[str, Any]:
    """Строгая схема. Все поля обязательны — strict json_schema иных не знает."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "n": {"type": "integer"},
                        # Не «подходит ли», а «что это». Первая версия спрашивала
                        # решение — и на вьетнамских объявлениях о домах модель
                        # три раза из трёх отвечала «подходит» для запроса про
                        # мотоцикл: правила были на русском, объявление на
                        # вьетнамском, и связать их она не смогла. Назвать
                        # предмет из закрытого списка она может на любом языке.
                        "subject": {
                            "type": "string",
                            "enum": [*[c.value for c in Category], "unknown"],
                        },
                        "keep": {"type": "boolean"},
                        "why": {"type": "string"},
                        # Строкой, а не числом: провайдеры возвращают то `400`,
                        # то `"400 000"`, и разобрать строку надёжнее, чем
                        # спорить о типе (та же причина, что в intake).
                        "price_vnd": {"type": "string"},
                        "price_text": {"type": "string"},
                    },
                    "required": ["n", "subject", "keep", "why", "price_vnd", "price_text"],
                },
            }
        },
        "required": ["verdicts"],
    }


async def screen(
    passport: Passport,
    items: list[RawItem],
    *,
    usd_vnd: float | None = None,
    broker: BrokerClient | None = None,
    limit: int = GUARD_ITEMS,
) -> list[RawItem]:
    """Проверенная выдача. Наружу не бросает: отказ означает выдачу как есть."""
    head = items[:limit]
    if not head:
        return items

    own = broker is None
    client = broker or BrokerClient(usage=default_usage_sink)
    try:
        payload = await asyncio.wait_for(
            client.structured(
                _prompt(passport, head, usd_vnd),
                schema=guard_schema(),
                schema_name="listing_guard",
                capability=GUARD_CAPABILITY,
                system=SYSTEM,
                max_tokens=GUARD_TOKENS_PER_ITEM * len(head) + GUARD_TOKENS_OVERHEAD,
            ),
            timeout=GUARD_TIMEOUT_S,
        )
    except (BrokerError, TimeoutError, OSError) as exc:
        # Улучшение, а не условие работы: выдача уходит непроверенной.
        log.warning("guard.unavailable", kind=type(exc).__name__, error=str(exc)[:200])
        return items
    finally:
        if own:
            await client.aclose()

    verdicts = _verdicts(payload, head)
    wanted = passport.category.value if passport.category else None
    kept = [
        _apply(item, verdicts.get(number), wanted=wanted)
        for number, item in enumerate(head, start=1)
    ]
    ceiling = _ceiling_vnd(passport, usd_vnd)
    passed = [item for item in kept if item is not None and _affordable(item, ceiling)]
    log.info(
        "guard.screened",
        checked=len(head),
        dropped=len(head) - len(passed),
        reasons=[v.reason for v in verdicts.values() if not v.keep][:5],
    )
    # Хвост НЕ возвращается, и это исправление, а не экономия. Сначала он ехал
    # следом за проверенным — «о нём ничего не известно, выбрасывать не за что».
    # Замер 01.09.2026 показал, чем это оборачивается: охранник снял десять
    # карточек из двенадцати, и наверх всплыл непроверенный хвост, куда
    # ранжирование сложило заведомо худшее. На «мотоцикл до 300 USD» клиент
    # увидел ноутбуки ThinkPad — не потому что охранник их пропустил, а потому
    # что до них он не дошёл.
    #
    # Проверяем двенадцать, показываем пять (`MAX_CARDS`) — запас двукратный.
    # Три проверенные карточки честнее, чем три проверенные и две неизвестно
    # какие: для клиента они выглядят одинаково.
    return passed


def _same_subject(subject: str, wanted: str | None) -> bool:
    """Тот ли предмет. Сравнение строк — работа кода, а не модели.

    Замер 01.09.2026: на пяти вьетнамских объявлениях о домах и земле модель
    три раза из трёх ответила «подходит» для запроса про мотоцикл. Правила
    отказа были перечислены по-русски («телефон, ноутбук, часы, колонка»),
    объявление написано по-вьетнамски (`Bán nhà`, `căn hộ`, `Bán đất`), и связь
    между ними она не построила. Дописать вьетнамские слова в список значило бы
    ждать следующего языка.

    Поэтому вопрос к модели теперь другой: не «подходит ли», а «что это» —
    одним значением из нашего закрытого списка. Назвать предмет она умеет на
    любом языке; сравнить две строки умеем мы.

    `unknown` проходит: непонятное объявление решает человек, а не мы за него.
    """
    if wanted is None or subject in ("", "unknown"):
        return True
    return subject == wanted


def _affordable(item: RawItem, ceiling: int | None) -> bool:
    """Влезает ли находка в бюджет. Сравнение чисел — работа кода, не модели.

    Первая версия поручала это модели вместе со смыслом, и замер 01.09.2026
    показал, почему так нельзя: на «до 300 USD» (потолок 7.8 млн) она оставила
    байки за 10–11 млн, а на «до 400 USD» (потолок 10.4 млн) отбраковала ВСЁ.
    Один и тот же промпт, соседние запросы, противоположная строгость — это
    разброс, а не ошибка формулировки, и лечится он тем, что арифметику у
    модели забирают.

    Запас в 30% — тот же, что у ранжирования (`search/relevance.py`): чуть
    дороже бюджета клиент вправе увидеть и решить сам, вдесятеро дороже — нет.
    Находка без цены проходит: «цену не написали» не значит «дорого».
    """
    if ceiling is None or item.price_vnd is None:
        return True
    return item.price_vnd <= ceiling * PRICE_TOLERANCE


def _apply(item: RawItem, verdict: Verdict | None, *, wanted: str | None) -> RawItem | None:
    """Вердикт на карточку. `None` — карточка не показывается.

    Молчание модели о карточке — не отказ: пропущенный номер в ответе означает,
    что её не проверили, а не что она плохая.
    """
    if verdict is None:
        return item
    if not verdict.keep or not _same_subject(verdict.subject, wanted):
        return None
    if verdict.price_vnd is None or not verdict.price_text:
        return item
    if verdict.price_text not in item.text:
        # Фрагмент, которого в объявлении нет, — придуманный. Молча выкидываем
        # цену, карточку оставляем: она могла быть верной и без цены.
        log.warning("guard.price_not_grounded", url=item.url, quoted=verdict.price_text[:60])
        return item
    if item.price_vnd is not None:
        return item
    item.price_vnd = verdict.price_vnd
    if not item.price_raw.strip():
        item.price_raw = verdict.price_text
    return item


def _verdicts(payload: dict[str, Any], head: list[RawItem]) -> dict[int, Verdict]:
    parsed: dict[int, Verdict] = {}
    for row in payload.get("verdicts", []):
        if not isinstance(row, dict):
            continue
        number = _int(row.get("n"))
        if number is None or not 1 <= number <= len(head):
            continue
        parsed[number] = Verdict(
            keep=bool(row.get("keep", True)),
            reason=str(row.get("why", ""))[:120],
            subject=str(row.get("subject", "unknown")).strip().lower(),
            price_text=str(row.get("price_text", "")).strip(),
            price_vnd=_price(row.get("price_vnd")),
        )
    return parsed


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _price(value: object) -> int | None:
    """Число из строки. Разделители у провайдеров любые, значение — донги."""
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if not digits:
        return None
    amount = int(digits)
    if not 0 < amount <= MAX_PLAUSIBLE_VND:
        return None
    return amount


def _prompt(passport: Passport, items: list[RawItem], usd_vnd: float | None) -> str:
    lines = [f"Клиент ищет: {_wanted(passport, usd_vnd)}", "", "Объявления:"]
    for number, item in enumerate(items, start=1):
        text = " ".join((item.title + " " + item.text).split())[:GUARD_TEXT_CHARS]
        lines.append(f"{number}) {text}")
    lines += [
        "",
        "Про каждое объявление ответь, ЧТО в нём продают или сдают —",
        "одним значением: motorbike, bicycle, car, apartment, room, house,",
        "other. Не понял — unknown. Объявление может быть на русском,",
        "вьетнамском или английском; отвечай значением из этого списка.",
        "",
        "keep=false ставь, только если объявление явно не о поиске клиента",
        "и это видно без домыслов. Совпадение предмета проверят без тебя:",
        "— цену с бюджетом НЕ сравнивай, это сделают без тебя;",
        "— отсутствие цены НЕ причина отказа: её просто не написали;",
        "— непонятное или короткое объявление оставляй, решит человек.",
        "",
        "price_text — точный фрагмент объявления с ценой, слово в слово,",
        "или пустая строка, если цены в тексте нет. Не пересказывай.",
        "price_vnd — та же сумма в донгах цифрами.",
        "why — до восьми слов, почему решил так.",
    ]
    return "\n".join(lines)


def _wanted(passport: Passport, usd_vnd: float | None) -> str:
    """Запрос словами. Бюджет — в донгах: объявления написаны в них."""
    parts: list[str] = []
    if passport.intent:
        parts.append(INTENT_WORDS.get(passport.intent, passport.intent.value))
    if passport.category:
        # Русскими словами, а не значением перечисления. Замер 01.09.2026: со
        # строкой `motorbike` модель отбраковывала скутеры с формулировкой «это
        # скутер, а клиент ищет motorbike» — она читала имя поля как требование,
        # потому что человек так не пишет.
        words = CATEGORY_TERMS.get(passport.category, {}).get("ru", ())
        parts.append(" / ".join(words) or passport.category.value)
    if passport.city:
        parts.append(CITY_WORDS.get(passport.city, passport.city))
    for field, value in passport.attributes.items():
        parts.append(f"{field}={value}")
    ceiling = _ceiling_vnd(passport, usd_vnd)
    if ceiling is not None:
        parts.append(f"бюджет до {ceiling:,} VND".replace(",", " "))
    return ", ".join(parts) or "не уточнено"


def _ceiling_vnd(passport: Passport, usd_vnd: float | None) -> int | None:
    budget = passport.budget
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return int(budget.max)
    if budget.currency is Currency.USD and usd_vnd is not None:
        return int(budget.max * usd_vnd)
    # Валюту знаем, курса нет — врать про потолок нельзя: скажем, что бюджета
    # не знаем, и охранник проверит только смысл.
    return None
