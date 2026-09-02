"""Каталог поддельного рынка Нячанга: находки вместе с правдой о них.

Подделка нарочно грязная. Отдай она только идеальные совпадения — харнес мерил
бы себя: любая выдача выглядела бы безупречной, а «показывает не то» из жалобы
владельца не воспроизвелось бы никогда. Поэтому здесь лежат ровно те находки,
из-за которых на выдачу и жалуются: Airblade на запрос Lead, лот 59 дней от
роду, вариант вдвое дороже названного бюджета и чужая категория, которую живой
поиск по чату вытаскивает по одному общему слову.

Правда о лоте (марка, модель, коробка, объём) лежит РЯДОМ с `RawItem`, а не
внутри него: источник таких полей не отдаёт — он отдаёт текст. Это знание
симулятора, и смешивать его с тем, что видит бот, нельзя, иначе бот получит
подсказку, которой в бою нет. Судят этой правдой в `fit.py`, отдельно: мерку не
должно быть можно поправить тем же движением, что каталог.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sniffer.domain.passport import Category
from sniffer.sources.base import RawItem


@dataclass(frozen=True, slots=True)
class Lot:
    """Находка плюс правда о ней. Боту достаётся только `item`."""

    item: RawItem
    category: Category
    brand: str = ""
    model: str = ""
    transmission: str = ""
    engine_cc: int | None = None


def _lot(
    key: str,
    title: str,
    *,
    category: Category,
    price_vnd: int,
    age_days: int,
    source: str = "chotot",
    brand: str = "",
    model: str = "",
    transmission: str = "",
    engine_cc: int | None = None,
) -> Lot:
    url = (
        f"https://www.chotot.com/{key}.htm"
        if source == "chotot"
        else f"https://t.me/nhatrang_baraholka/{key}"
    )
    return Lot(
        item=RawItem(
            source=source,
            external_id=key,
            url=url,
            title=title,
            price_raw=f"{price_vnd:,} đ".replace(",", "."),
            price_vnd=price_vnd,
            # Возраст, а не дата: карточка и ранжирование считают его от
            # текущего момента, и фиксированная дата состарила бы каталог сама.
            posted_at=datetime.now(UTC) - timedelta(days=age_days),
        ),
        category=category,
        brand=brand,
        model=model,
        transmission=transmission,
        engine_cc=engine_cc,
    )


def build_catalog() -> tuple[Lot, ...]:
    """Каталог собирается вызовом, потому что возраст лотов отсчитывается от «сейчас»."""
    bike, flat, room, house, cycle = (
        Category.MOTORBIKE,
        Category.APARTMENT,
        Category.ROOM,
        Category.HOUSE,
        Category.BICYCLE,
    )
    return (
        # ── мотобайки ───────────────────────────────────────────────────────
        _lot(
            "honda-lead-2018",
            "Honda Lead 125 2018, автомат, синий",
            category=bike,
            price_vnd=12_000_000,
            age_days=2,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
        # Тот же Lead, но 59 дней от роду: в Нячанге его продали ещё в прошлом
        # месяце, а объявление висит. Ради него в карточке и живёт пометка.
        _lot(
            "honda-lead-old",
            "Honda Lead 125, автомат, документы в порядке",
            category=bike,
            price_vnd=11_500_000,
            age_days=59,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
        # Airblade на запрос Lead: обе Honda, обе автомат, разница только в
        # модели — ровно тот случай, ради которого модель отделяют от марки.
        _lot(
            "honda-airblade-2020",
            "Honda Air Blade 125 2020, автомат",
            category=bike,
            price_vnd=13_500_000,
            age_days=3,
            brand="honda",
            model="airblade",
            transmission="automatic",
            engine_cc=125,
        ),
        _lot(
            "honda-vision-2019",
            "Honda Vision 2019, автомат, один хозяин",
            category=bike,
            price_vnd=9_800_000,
            age_days=5,
            brand="honda",
            model="vision",
            transmission="automatic",
            engine_cc=110,
        ),
        _lot(
            "yamaha-exciter-155",
            "Yamaha Exciter 155 VVA, механика",
            category=bike,
            price_vnd=24_000_000,
            age_days=4,
            brand="yamaha",
            model="exciter",
            transmission="manual",
            engine_cc=155,
        ),
        _lot(
            "yamaha-sirius",
            "Yamaha Sirius, механика, недорого",
            category=bike,
            price_vnd=7_200_000,
            age_days=12,
            source="telegram_groups",
            brand="yamaha",
            model="sirius",
            transmission="manual",
            engine_cc=110,
        ),
        _lot(
            "honda-cb200x",
            "Honda CB200X, 200 кубов, механика",
            category=bike,
            price_vnd=46_000_000,
            age_days=6,
            brand="honda",
            model="cb200x",
            transmission="manual",
            engine_cc=200,
        ),
        _lot(
            "vespa-sprint",
            "Vespa Sprint 150 2022, автомат, как новая",
            category=bike,
            price_vnd=62_000_000,
            age_days=9,
            brand="vespa",
            model="sprint",
            transmission="automatic",
            engine_cc=150,
        ),
        _lot(
            "honda-wave-alpha",
            "Honda Wave Alpha, механика, на ходу",
            category=bike,
            price_vnd=6_500_000,
            age_days=21,
            source="telegram_groups",
            brand="honda",
            model="wave",
            transmission="manual",
            engine_cc=110,
        ),
        # ── жильё ───────────────────────────────────────────────────────────
        _lot(
            "apt-studio-muong",
            "Студия с мебелью, Мыонг Тхань, длительный срок",
            category=flat,
            price_vnd=8_000_000,
            age_days=2,
            source="telegram_groups",
        ),
        _lot(
            "apt-two-rooms",
            "2 спальни, кондиционеры, стиральная машина",
            category=flat,
            price_vnd=14_000_000,
            age_days=4,
            source="telegram_groups",
        ),
        _lot(
            "apt-seaview-lux",
            "Апартаменты с видом на море, бассейн, спортзал",
            category=flat,
            price_vnd=25_000_000,
            age_days=30,
        ),
        _lot(
            "room-shared-kitchen",
            "Комната с общей кухней, район Винком",
            category=room,
            price_vnd=4_500_000,
            age_days=3,
            source="telegram_groups",
        ),
        _lot(
            "house-villa-an-vien",
            "Дом в Ан Виен, 3 спальни, длительный срок",
            category=house,
            price_vnd=45_000_000,
            age_days=8,
        ),
        # ── чужая категория, которую источник тянет по общему слову ─────────
        _lot(
            "giant-escape-3",
            "Велосипед Giant Escape 3, почти новый",
            category=cycle,
            price_vnd=3_200_000,
            age_days=10,
            source="telegram_groups",
        ),
    )


CATALOG: tuple[Lot, ...] = build_catalog()
