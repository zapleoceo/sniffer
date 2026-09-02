"""Бюджет и паспорт должны менять карточки, а не оставаться текстом в логе."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.search.relevance import rank_items, with_vnd_budget
from sniffer.sources.base import RawItem

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RATE = 26_000.0


def passport(**changes: Any) -> Passport:
    values: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=500, currency=Currency.USD),
        "attributes": {"transmission": "automatic"},
    }
    values.update(changes)
    return Passport(**values)


def item(
    name: str,
    *,
    price: int | None,
    text: str = "",
    age_hours: int = 1,
) -> RawItem:
    return RawItem(
        source="telegram_groups",
        external_id=name,
        url=f"https://example.test/{name}",
        title=name,
        text=text,
        price_vnd=price,
        posted_at=NOW - timedelta(hours=age_hours),
    )


def test_usd_budget_becomes_vnd_before_chotot_request() -> None:
    plan = SearchPlan(tasks=[SearchTask(source="chotot", query="", params={"budget": {}})])

    converted = with_vnd_budget(plan, passport(), RATE)

    assert converted.tasks[0].params["budget"] == {
        "min": 0.0,
        "max": 13_000_000.0,
        "currency": "VND",
        "period": "month",
    }


def test_budget_beats_recency_in_card_order() -> None:
    expensive = item("new but too expensive", price=18_000_000, age_hours=1)
    suitable = item("older but suitable", price=12_000_000, age_hours=24)

    ordered = rank_items(passport(), [expensive, suitable], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == ["older but suitable"]


def test_known_automatic_text_beats_unconfirmed_variant() -> None:
    confirmed = item("automatic", price=12_000_000, text="Xe tay ga Honda", age_hours=12)
    unknown = item("unknown", price=12_000_000, text="Honda 125", age_hours=1)

    ordered = rank_items(passport(), [unknown, confirmed], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == ["automatic", "unknown"]


def test_missing_rate_never_invents_price_filter() -> None:
    original = SearchPlan(tasks=[SearchTask(source="chotot", query="")])

    assert with_vnd_budget(original, passport(), None) == original


# ── чужая категория до карточек не доходит ─────────────────────────────────


def test_another_category_does_not_reach_the_cards() -> None:
    """Комната и велосипед в выдаче скутеров — 41% показанного в замере 02.09.2026.

    Поиск по чату идёт словами, структурного поля категории у чата нет, а
    модельный фильтр такой лот не видит: комната модели не называет. В пятёрку
    она проходила по свежести.
    """
    room = item("Комната с общей кухней, район Винком", price=4_500_000)
    bike = item("Велосипед Giant Escape 3, почти новый", price=3_200_000)
    scooter = item("Honda Vision 2019, скутер", price=9_800_000, age_hours=48)

    ordered = rank_items(passport(), [room, bike, scooter], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == [scooter.external_id]


def test_a_lot_whose_category_is_unreadable_is_not_thrown_away() -> None:
    """«Не смогли прочитать» — это не «не подходит».

    То же правило, что у модели: продавец не обязан называть предмет словом из
    нашего словаря, и выбрасывать за это значит терять половину чата. Лот назван
    так, что категорию не выдаёт НИ слово, НИ модель — иначе `category_of`
    прочитал бы её из имени модели, и «неизвестности» бы не осталось.
    """
    silent = item("Продам срочно, недорого, один хозяин, торг. Звоните", price=9_800_000)

    assert rank_items(passport(), [silent], usd_vnd=RATE, now=NOW) == [silent]


def test_a_bike_named_only_by_its_model_is_kept_out_of_a_flat_search() -> None:
    """«Honda Lead 110 2008» без слова «скутер» — всё равно мотобайк.

    Категория лота читается с тем же выводом из модели, что и запрос: иначе
    байк, назвавший лишь модель, для запроса о жилье выглядел «неизвестной
    категорией» и проходил фильтр (realcheck 03.09.2026 — Honda Lead в выдаче
    студий). Модель Lead бывает только у мотобайка, и на запрос квартиры лот
    отсеивается, хоть слова «скутер» в нём и нет.
    """
    bike = item("Honda Lead 110 2008, пробег 20к, вариатор", price=10_000_000)
    flat = item("Сдам студию у моря, мебель, длительно", price=9_000_000, age_hours=72)

    ordered = rank_items(passport(category=Category.APARTMENT), [bike, flat], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == [flat.external_id]


def test_an_empty_result_does_not_bring_the_foreign_category_back() -> None:
    """Ступень не отменяется при пустом результате — в отличие от возраста.

    Возраст — догадка о живости, чужая категория — факт о предмете, прочитанный
    из его собственных слов. Комната вместо скутера, когда скутеров нет, — это
    ровно та жалоба, ради которой отсев и появился: пустой ответ хотя бы
    советует переформулировать.
    """
    room = item("Сдам комнату, район Винком", price=4_500_000)

    assert rank_items(passport(), [room], usd_vnd=RATE, now=NOW) == []


def test_a_request_without_a_category_filters_nothing() -> None:
    """Клиент не назвал предмет — сравнивать не с чем, и выбрасывать не за что."""
    room = item("Комната с общей кухней", price=4_500_000)

    assert rank_items(passport(category=None), [room], usd_vnd=RATE, now=NOW) == [room]


# ── чужая марка и коробка тоже не доходят ──────────────────────────────────


def test_a_yamaha_request_drops_a_honda_lot() -> None:
    """realcheck 03.09.2026: на «ямаха» приходили Honda и Kymco.

    Марку лота фильтр читал словарём фраз, где марок нет ВОВСЕ (там свойства —
    коробка, документы, состояние), поэтому фильтр по марке был мёртв: Honda
    приходила на «ямаха». Симуляция это пропускала — состязательной смеси в её
    рынке не было. Теперь марка лота читается тем же детектором, что и запрос.
    """
    honda = item("off_brand", price=12_000_000, text="Honda Vision 2019, автомат")
    yamaha = item("on_brand", price=12_000_000, text="Yamaha Janus, автомат")

    ordered = rank_items(
        passport(attributes={"brand": "yamaha"}), [honda, yamaha], usd_vnd=RATE, now=NOW
    )

    assert [candidate.external_id for candidate in ordered] == ["on_brand"]


def test_a_yamaha_request_drops_a_kymco_lot() -> None:
    """Kymco — 147 объявлений в базе Нячанга (motorbike_models).

    Пока его не было в словаре марок, он читался «неизвестной маркой» и молча
    проходил на «ямаха»: неполнота словаря неотличима от «марка не названа».
    """
    kymco = item("off_brand", price=12_000_000, text="Kymco Like 125, автомат")
    yamaha = item("on_brand", price=12_000_000, text="Yamaha Nouvo, автомат")

    ordered = rank_items(
        passport(attributes={"brand": "yamaha"}), [kymco, yamaha], usd_vnd=RATE, now=NOW
    )

    assert [candidate.external_id for candidate in ordered] == ["on_brand"]


def test_a_lot_with_no_readable_brand_survives_a_brand_request() -> None:
    """Неизвестное — не противоречие: лот, не назвавший марку, остаётся.

    Та же дисциплина, что у категории и модели. Иначе фильтр по марке терял бы
    половину чата, где продавец пишет «продам байк», а бренд не называет.
    """
    silent = item("silent", price=12_000_000, text="Продам байк, один хозяин, автомат")

    assert rank_items(passport(attributes={"brand": "honda"}), [silent], usd_vnd=RATE, now=NOW) == [
        silent
    ]


def test_a_honda_request_drops_a_yamaha_named_only_by_its_model() -> None:
    """Марку `detect_brand` выводит и из модели: «Exciter» — это Yamaha.

    Поэтому лот отсеётся на запрос honda, даже не написав «yamaha» словом, —
    ровно как марка выводится из модели в разборе запроса клиента.
    """
    exciter = item("by_model", price=12_000_000, text="Продам Exciter 155, срочно")

    assert (
        rank_items(passport(attributes={"brand": "honda"}), [exciter], usd_vnd=RATE, now=NOW) == []
    )


def test_an_automatic_request_drops_manual_lots_in_either_language() -> None:
    """realcheck: на «автомат» приходила механика (Winner, R15).

    Коробку лота фильтр читал тем же несуществующим словарём фраз, что и марку.
    Обе оси проверяем разом: «механика» и вьетнамское «côn tay» — одно знание.
    """
    ru_manual = item("ru_manual", price=12_000_000, text="Yamaha Exciter, механика")
    vi_manual = item("vi_manual", price=12_000_000, text="Sirius côn tay, chính chủ")
    automatic = item("kept", price=12_000_000, text="Honda Lead, автомат")

    ordered = rank_items(passport(), [ru_manual, vi_manual, automatic], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == ["kept"]


def test_an_automatic_request_keeps_a_lot_silent_on_the_gearbox() -> None:
    """Отсутствие слова о коробке — не механика.

    Chotot мог отобрать коробку структурным полем, а продавец не обязан
    повторять её в заголовке. Молчание — не несовпадение (spec-v2, 3.3).
    """
    silent = item("silent", price=12_000_000, text="Honda Vision 2019, один хозяин")

    assert rank_items(passport(), [silent], usd_vnd=RATE, now=NOW) == [silent]


def test_a_yamaha_request_keeps_every_yamaha() -> None:
    """Контроль: фильтр отсекает чужое, а не всё подряд.

    Без этой проверки «починка», роняющая заодно и верные Ямахи, прошла бы
    зелёной: лечение оказалось бы хуже болезни, и заметить это было бы нечем.
    """
    janus = item("janus", price=12_000_000, text="Yamaha Janus, автомат")
    nouvo = item("nouvo", price=12_000_000, text="Yamaha Nouvo, автомат")
    exciter = item("exciter", price=12_000_000, text="Yamaha Exciter, механика")

    got = rank_items(
        passport(attributes={"brand": "yamaha"}), [janus, nouvo, exciter], usd_vnd=RATE, now=NOW
    )

    assert {candidate.external_id for candidate in got} == {"janus", "nouvo", "exciter"}
