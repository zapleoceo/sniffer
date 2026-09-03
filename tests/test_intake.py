"""Разбор формулировки клиента в паспорт.

Эвристика проверяется на тех фразах, которыми люди действительно пишут в
Нячанге: две валюты, миллионы донгов, падежные окончания и год выпуска рядом с
ценой. Модель замокана — тест, который ходит в брокер, не тест.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sniffer.broker.client import BrokerCapError, BrokerError
from sniffer.config import Settings
from sniffer.domain.passport import Category, Currency, Intent, PassportStatus, PricePeriod
from sniffer.search import intake as intake_module
from sniffer.search.intake import QueryIntake, intake_schema, merge
from sniffer.search.intake_rules import (
    detect_category,
    detect_rooms,
    detect_transmission,
    parse_query,
)
from sniffer.search.market_terms import ATTRIBUTE_TERMS

CITY = "nha_trang"


class FakeBroker:
    """Подменяет только `structured` — больше разбору от брокера не нужно."""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    async def structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return dict(self.payload) if isinstance(self.payload, dict) else self.payload


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение без ключа брокера — иначе тест зависел бы от чужого `.env`."""
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city=CITY),
    )


@pytest.mark.parametrize(
    ("text", "category", "intent", "budget_max", "currency", "period"),
    [
        (
            "ищу скутер в Нячанге до 400 долларов",
            Category.MOTORBIKE,
            Intent.BUY,
            400,
            Currency.USD,
            PricePeriod.ONCE,
        ),
        (
            "сниму квартиру в Нячанге до 10 млн донгов",
            Category.APARTMENT,
            Intent.RENT,
            10_000_000,
            Currency.VND,
            PricePeriod.MONTH,
        ),
        (
            "куплю байк до 400$",
            Category.MOTORBIKE,
            Intent.BUY,
            400,
            Currency.USD,
            PricePeriod.ONCE,
        ),
        (
            "нужна комната до 5 млн",
            Category.ROOM,
            # «Нужна» говорит, что клиент ищет, но не говорит, покупает он или
            # снимает: комнату в Нячанге снимают.
            Intent.RENT,
            5_000_000,
            Currency.VND,
            PricePeriod.MONTH,
        ),
        (
            "сниму студию до 300 usd в месяц",
            Category.APARTMENT,
            Intent.RENT,
            300,
            Currency.USD,
            PricePeriod.MONTH,
        ),
        (
            "ищу велосипед до 100 долларов",
            Category.BICYCLE,
            Intent.BUY,
            100,
            Currency.USD,
            PricePeriod.ONCE,
        ),
    ],
    ids=["usd_scooter", "vnd_flat", "dollar_sign", "millions_no_currency", "studio", "bicycle"],
)
def test_rules_read_the_query(
    text: str,
    category: Category,
    intent: Intent,
    budget_max: float,
    currency: Currency,
    period: PricePeriod,
) -> None:
    passport = parse_query(text, default_city=CITY)

    assert passport.category is category
    assert passport.intent is intent
    assert passport.budget.max == budget_max
    assert passport.budget.currency is currency
    assert passport.budget.period is period
    assert passport.city == CITY
    assert passport.raw_query == text


def test_range_becomes_both_bounds() -> None:
    passport = parse_query("ищу квартиру в аренду от 300 до 500 долларов", default_city=CITY)

    assert passport.budget.min == 300
    assert passport.budget.max == 500


def test_year_is_not_a_price() -> None:
    """«2019 года» — это год выпуска, а не бюджет в две тысячи долларов."""
    passport = parse_query("продам скутер 2019 года", default_city=CITY)

    assert passport.intent is Intent.SELL
    assert passport.budget.max is None


def test_brand_reaches_attributes() -> None:
    """Бренд оттуда попадает первым запросом в шаблонный план поиска."""
    passport = parse_query("куплю honda vision до 400$", default_city=CITY)

    assert passport.attributes["brand"] == "honda"


def test_a_bare_brand_derives_the_motorbike_category() -> None:
    """«yamaha» без иных слов — мотобайк: все марки рынка мотобайковые.

    Живой отказ 02.09.2026: на «yamaha» категория оставалась пустой (`None`), и
    отсев чужой категории не работал — в выдачу лезла даже квартира. Категория
    выводится из марки так же, как из модели.
    """
    passport = parse_query("yamaha", default_city=CITY)

    assert passport.category is Category.MOTORBIKE
    assert passport.attributes["brand"] == "yamaha"


def test_a_said_category_outranks_a_brand_mentioned_in_passing() -> None:
    """«сниму квартиру рядом с Honda» — жильё, а не байк.

    Вывод из марки ложится ТОЛЬКО на пустое место: сказанное клиентом словом
    («квартиру») главнее марки, упомянутой мимоходом. Иначе салон Honda по
    соседству превратил бы аренду квартиры в поиск мотобайка.
    """
    passport = parse_query("сниму квартиру рядом с Honda", default_city=CITY)

    assert passport.category is Category.APARTMENT
    assert passport.intent is Intent.RENT


def test_city_from_text_wins_over_default() -> None:
    passport = parse_query("сдам квартиру в Дананге 8 млн в месяц", default_city=CITY)

    assert passport.city == "da_nang"
    assert passport.intent is Intent.RENT_OUT


@pytest.mark.parametrize(
    ("text", "city"),
    [
        ("ищу скутер в Хойане", "hoi_an"),
        ("сниму квартиру в Вунгтау", "vung_tau"),
        ("ищу байк в Далате", "da_lat"),
        ("ищу скутер в Ханое", "ha_noi"),
        ("ищу скутер в Сайгоне", "ho_chi_minh"),
        ("ищу скутер в Хошимине", "ho_chi_minh"),
        ("scooter in Hoi An", "hoi_an"),
    ],
    ids=["hoi_an", "vung_tau", "da_lat", "ha_noi", "saigon", "hcmc", "latin"],
)
def test_city_we_do_not_serve_is_still_recognised(text: str, city: str) -> None:
    """Город, где мы не ищем, обязан попасть в паспорт своим слагом.

    Падало до правки: справочник знал два города, остальные подставлялись
    городом по умолчанию — и запрос про Хойан приходил как запрос про Нячанг,
    то есть неотличимо от повтора прежней просьбы.
    """
    assert parse_query(text, default_city=CITY).city == city


def test_unclear_query_still_gives_passport() -> None:
    """Категорию не узнали — ищем словами клиента, а не отказываемся."""
    passport = parse_query("ищу холодильник", default_city=CITY)

    assert passport.category is None
    assert passport.status is PassportStatus.DRAFT
    assert "category" in passport.missing_fields
    assert passport.raw_query == "ищу холодильник"


# ── коробка передач: сказана в запросе, а не спрошена кнопкой ───────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("скутер автомат до 500 долларов", "automatic"),
        ("байк на автомате", "automatic"),
        ("хочу скутер с вариатором", "automatic"),
        ("нужен байк, механику", "manual"),
        ("мотоцикл на механике", "manual"),
        ("скутер полуавтомат", "semi"),
        ("xe máy tay ga", "automatic"),
        ("xe côn tay nha trang", "manual"),
        ("cần xe số", "semi"),
        ("looking for an automatic scooter", "automatic"),
        ("manual motorbike", "manual"),
    ],
    ids=[
        "ru_auto",
        "ru_auto_case",
        "ru_variator",
        "ru_manual_case",
        "ru_manual_prepositional",
        "ru_semi",
        "vi_auto",
        "vi_manual",
        "vi_semi",
        "en_auto",
        "en_manual",
    ],
)
def test_the_gearbox_is_read_from_the_query_itself(text: str, expected: str) -> None:
    """Клиент сказал «автомат» — переспрашивать это кнопкой значит не слушать.

    Три языка рынка и падежи: «на автомате», «механику». Слова берутся из
    словаря рынка, поэтому список здесь — формулировки клиента, а не копия
    словаря; саму копию сторожит тест ниже.
    """
    assert parse_query(text, default_city=CITY).attributes["transmission"] == expected


@pytest.mark.parametrize(
    ("value", "term"),
    [
        (value, term)
        for value, langs in ATTRIBUTE_TERMS[Category.MOTORBIKE]["transmission"].items()
        for terms in langs.values()
        for term in terms
    ],
)
def test_every_gearbox_word_of_the_market_dictionary_is_understood(value: str, term: str) -> None:
    """Второго списка слов о коробке в проекте нет — и этот тест не даёт ему завестись.

    Список в `parametrize` собран из самой таблицы: допишут слово — оно попадёт
    сюда само. Разбор, скопировавший слова к себе, разошёлся бы со словарём
    молча: ровно так «xe số» успело значить механику в одном месте и
    полуавтомат в другом.
    """
    assert detect_transmission(term) == value


def test_a_gearbox_is_not_read_where_the_category_has_no_gearbox() -> None:
    """«Стиральная машина автомат» — не коробка передач.

    Категорию спрашивает таблица атрибутов, а не ветка в коде: у жилья поля
    `transmission` нет вовсе, поэтому слово остаётся словом.
    """
    passport = parse_query("квартира со стиральной машиной автомат", default_city=CITY)

    assert passport.category is Category.APARTMENT
    assert "transmission" not in passport.attributes


async def test_without_broker_key_model_is_not_called(offline: None) -> None:
    broker = FakeBroker({"category": "car"})

    passport = await QueryIntake().parse("ищу скутер до 400 долларов")

    assert not broker.calls
    assert passport.category is Category.MOTORBIKE


async def test_model_answer_refines_rules(offline: None) -> None:
    broker = FakeBroker(
        {
            "intent": "buy",
            "category": "motorbike",
            "city": "da_nang",
            "budget_max": "450",
            "currency": "USD",
            "period": "once",
            "brand": "Honda",
        }
    )

    passport = await QueryIntake(broker).parse("ищу что-нибудь на двух колёсах")

    assert len(broker.calls) == 1
    assert passport.city == "da_nang"
    assert passport.budget.max == 450
    assert passport.attributes["brand"] == "honda"
    assert passport.confidence == 0.8


async def test_model_silence_does_not_erase_rules(offline: None) -> None:
    """Пустые поля модели не должны стирать то, что разобрано по словам."""
    broker = FakeBroker(
        {
            "intent": "",
            "category": "",
            "city": "",
            "budget_max": "",
            "currency": "",
            "period": "",
            "brand": "",
        }
    )

    passport = await QueryIntake(broker).parse("ищу скутер в Нячанге до 400 долларов")

    assert passport.category is Category.MOTORBIKE
    assert passport.city == CITY
    assert passport.budget.max == 400
    assert passport.budget.currency is Currency.USD


@pytest.mark.parametrize(
    "error",
    [
        BrokerError("провайдер вернул мусор"),
        BrokerCapError("daily budget cap reached"),
        httpx.ConnectError("брокер недоступен"),
        TimeoutError("не успел"),
    ],
    ids=["broker_error", "cap_reached", "connect_error", "timeout"],
)
async def test_broker_failure_falls_back_to_rules(offline: None, error: Exception) -> None:
    passport = await QueryIntake(FakeBroker(error=error)).parse("сниму комнату до 5 млн")

    assert passport.category is Category.ROOM
    assert passport.budget.max == 5_000_000


async def test_garbage_answer_falls_back_to_rules(offline: None) -> None:
    passport = await QueryIntake(FakeBroker("не объект вовсе")).parse("ищу скутер до 400$")

    assert passport.category is Category.MOTORBIKE
    assert passport.budget.max == 400


def test_schema_is_strict() -> None:
    schema = intake_schema()

    assert schema["additionalProperties"] is False
    # strict json_schema не допускает необязательных полей: «не знаю» модель
    # говорит пустой строкой.
    assert sorted(schema["required"]) == sorted(schema["properties"])
    assert "" in schema["properties"]["category"]["enum"]
    assert CITY in schema["properties"]["city"]["enum"]
    # Закрепи в перечислении только обслуживаемые города, и модель физически не
    # сможет сказать, что клиент назвал другой: отказ искать в Хойане — ответ,
    # молча подставленный Нячанг — нет.
    assert "hoi_an" in schema["properties"]["city"]["enum"]


def test_merge_ignores_unknown_values() -> None:
    rules = parse_query("ищу скутер до 400$", default_city=CITY)

    merged = merge(rules, {"category": "яхта", "currency": "BTC", "budget_max": "-5"})

    assert merged.category is Category.MOTORBIKE
    assert merged.budget.currency is Currency.USD
    assert merged.budget.max == 400


def test_papers_are_read_from_the_query() -> None:
    """«блюкарт»/«с блюкартом» → papers=blue_card, и падеж не мешает.

    Весь ручной поиск байка держался на блюкарте («без доков не надо»), а разбор
    его не читал вовсе — документное требование терялось, и документные лоты не
    поднимались в выдаче. Слова — из PAPERS_WORDS, тем же знанием, что и в тексте
    объявления.
    """
    assert (
        parse_query("байк с блюкартом", default_city=CITY).attributes.get("papers") == "blue_card"
    )
    assert (
        parse_query("скутер, документы есть", default_city=CITY).attributes.get("papers")
        == "blue_card"
    )


def test_papers_are_a_soft_signal_not_a_gearbox() -> None:
    """Документы — не коробка: отсутствие слова не заполняет и не отсеивает."""
    assert "papers" not in parse_query("нужен скутер honda lead", default_city=CITY).attributes


def test_a_malformed_number_does_not_crash_the_parse() -> None:
    """Разбор запроса зовётся на КАЖДОМ сообщении рынка и не вправе падать.

    Регексп суммы жадный и хватает почти-числа: живой отказ 02.09.2026 —
    «13.000.0002» (лишняя цифра в группе разрядов) ронял float() и весь
    parse_query, а с ним воркер на этом объявлении. Такая сумма просто не
    считается названной. Валидные числа рядом по-прежнему читаются.
    """
    assert parse_query("Honda Lead 13.000.0002 донг", default_city=CITY).budget.max is None
    assert parse_query("квартира 1.2.3 млн в нячанге", default_city=CITY).budget.max is None
    # Контроль: настоящий разделитель тысяч и дробь не сломаны.
    assert parse_query("скутер 5.000.000 VND", default_city=CITY).budget.max == 5_000_000
    assert parse_query("до 1,5 млн", default_city=CITY).budget.max == 1_500_000


def test_a_model_code_number_is_not_a_budget() -> None:
    """«Kawasaki z300» — это модель, а не «до 300». Живой отказ 02.09.2026.

    Клиент искал 300-кубовый мотоцикл Z300, а бот прочёл «300» бюджетом («до 300
    USD») и выдал 50cc «до 300 долларов». Число, склеенное с буквой, — код
    модели; из «cbr250» не должно вылезти и «50» (хвост за цифрой).
    """
    assert parse_query("Kawasaki z300", default_city=CITY).budget.max is None
    assert parse_query("honda cbr250 механика", default_city=CITY).budget.max is None
    assert parse_query("mt15", default_city=CITY).budget.max is None
    # Контроль: настоящий бюджет с пробелом по-прежнему читается.
    assert parse_query("нужен скутер до 300", default_city=CITY).budget.max == 300
    assert parse_query("байк 2019 года до 400", default_city=CITY).budget.max == 400


# ── число в счётном контексте — не бюджет (комнаты, срок) ────────────────────


@pytest.mark.parametrize(
    ("text", "budget_max"),
    [
        ("квартиру 2 спальни", None),
        ("аренда авто на 3 дня", None),
        ("сниму на 3 месяца", None),
        ("студию на 5 лет", None),
        ("2 bedroom apartment", None),
        ("двушку у моря", None),
        # Контроль: денежное число рядом со счётным остаётся бюджетом, а счётное
        # его не отменяет — «до 15 млн» несёт множитель, «2» нет.
        ("2 спальни до 15 млн", 15_000_000),
        ("квартиру 3 спальни до 500 долларов", 500),
        ("до 15 млн", 15_000_000),
    ],
    ids=[
        "bedrooms",
        "days",
        "months",
        "years",
        "en_bedroom",
        "kolloq",
        "count_plus_budget",
        "count_plus_usd",
        "plain_budget",
    ],
)
def test_a_counting_number_is_not_a_budget(text: str, budget_max: float | None) -> None:
    """«квартиру 2 спальни» → две спальни, а не «до 2 USD»; «на 3 дня» → срок.

    Число, за которым идёт счётная единица (спальни, дни, месяцы, годы), — не
    сумма. Тот же приём, что у года («2019 года»): цену и не-цену различает слово
    ПОСЛЕ числа. Денежное число рядом (с множителем «млн» или валютой) остаётся.
    """
    assert parse_query(text, default_city=CITY).budget.max == budget_max


# ── прокат — это аренда, а не покупка ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "прокат скутера",
        "прокат байка на месяц",
        "напрокат байк",
        "взять напрокат",
        "прокату мотоцикл",
    ],
)
def test_rental_words_are_rent_not_buy(text: str) -> None:
    """«прокат»/«напрокат» — клиент хочет ВЗЯТЬ, а не купить.

    Раньше он получал intent=buy, и отсев проката (`relevance`) выбрасывал ровно
    те лоты, что ему нужны. Теперь intent=rent, и клиент видит прокат.
    """
    assert parse_query(text, default_city=CITY).intent is Intent.RENT


# ── «жильё» — самый общий вид жилья ─────────────────────────────────────────


@pytest.mark.parametrize("form", ["жильё", "жилье", "жилья", "жильём"])
def test_the_housing_word_gives_the_apartment_category(form: str) -> None:
    """«жильё посуточно», «сниму жильё» — категория была пустой, теперь apartment.

    APARTMENT — самый общий вид жилья, поэтому «жильё» ведёт туда, а не в
    комнату/дом (они конкретнее и стоят в таблице позже).
    """
    assert parse_query(f"сниму {form}", default_city=CITY).category is Category.APARTMENT


def test_the_housing_word_does_not_catch_lookalikes() -> None:
    """«жилой» (через «о») и «жильцы» (через «ц») — не «жильё»: категории не дают."""
    assert detect_category("тихий жилой район") is None
    assert detect_category("шумные жильцы за стеной") is None


# ── атрибуты жилья из запроса: комнаты, мебель, вид на море ──────────────────


@pytest.mark.parametrize(
    ("text", "rooms"),
    [
        ("снять студию у моря", 1),
        ("однушку с мебелью", 1),
        ("квартиру 2 спальни", 2),
        ("двушка в центре", 2),
        ("2-комнатную квартиру", 2),
        ("трёшку у моря", 3),
        ("квартиру 3 спальни", 3),
        ("1 bedroom apartment for rent", 1),
        ("2 bedrooms apartment", 2),
    ],
)
def test_rooms_are_read_from_the_query(text: str, rooms: int) -> None:
    """Число комнат приезжает атрибутом — и словом, и цифрой, и латиницей."""
    assert parse_query(text, default_city=CITY).attributes.get("rooms") == rooms


def test_furnished_and_sea_view_are_read_from_the_query() -> None:
    """Мебель и вид на море — из словаря рынка, тем же `_attribute_named_in`."""
    both = parse_query("сниму квартиру у моря с мебелью", default_city=CITY)

    assert both.attributes["furnished"] is True
    assert both.attributes["sea_view"] is True
    assert parse_query("квартира без мебели", default_city=CITY).attributes["furnished"] is False
    assert parse_query("студия с видом на море", default_city=CITY).attributes["sea_view"] is True


def test_housing_attributes_belong_to_the_housing_category() -> None:
    """У мотобайка комнат и мебели нет — набор атрибутов принадлежит категории.

    Категорийно, а не ветвлением: тот же гейт, что у коробки передач. Абсурдный
    «скутер 2 спальни» служит проверкой — ни один жилой атрибут не заводится, и
    «2 спальни» при этом не становится бюджетом.
    """
    p = parse_query("скутер 2 спальни с мебелью у моря", default_city=CITY)

    assert p.category is Category.MOTORBIKE
    assert "rooms" not in p.attributes
    assert "furnished" not in p.attributes
    assert "sea_view" not in p.attributes
    assert p.budget.max is None


def test_detect_rooms_is_gated_by_the_category() -> None:
    """Тот же текст даёт комнаты жилью и молчит транспорту — таблица, не ветка."""
    assert detect_rooms("2 спальни", Category.APARTMENT) == 2
    assert detect_rooms("2 спальни", Category.MOTORBIKE) is None
    assert detect_rooms("2 спальни", None) is None


def test_the_rental_term_becomes_the_price_period() -> None:
    """Срок аренды — период цены: «посуточно» → day, «длительный срок» → month.

    Отдельного атрибута `term` нет: период уже есть в схеме, и «посуточно» это
    ровно «цена за сутки». Число срока при этом бюджетом не становится (см. выше).
    """
    assert parse_query("жильё посуточно", default_city=CITY).budget.period is PricePeriod.DAY
    assert (
        parse_query("снять квартиру на длительный срок", default_city=CITY).budget.period
        is PricePeriod.MONTH
    )
