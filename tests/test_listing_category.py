"""Категория объявления — предмет, названный первым, с падежами и без прилагательных.

Живые формулировки из базы 18.09.2026: квартиры, записанные мотобайком из-за
«рядом с Honda Nha Trang» и «парковки для байка», «1-комнатная квартира»,
прочитанная как комната, и «аренда байков», не узнанная вовсе.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.domain.passport import Category
from sniffer.domain.records import Listing
from sniffer.pipeline.gate import gate
from sniffer.search import vocabulary
from sniffer.search.intake_rules import category_of, detect_category
from sniffer.worker.recategorize import Recategorize, corrected


@pytest.mark.parametrize(
    ("text", "category"),
    [
        (
            "💥 СДАЁТСЯ 1-КОМНАТНАЯ КВАРТИРА — рядом с HONDA NHA TRANG, парковка для байка",
            Category.APARTMENT,
        ),
        ("LVCC Cho thuê căn hộ 1 phòng ngủ, chỗ để xe máy, giá 10.000.000", Category.APARTMENT),
        ("Продам Honda Lead 2019, доставлю к вашей квартире, цена 12 млн", Category.MOTORBIKE),
        ("Аренда байков, доставка к отелю и квартире, 150к в сутки", Category.MOTORBIKE),
        ("Скутеры в аренду от 120к в сутки, шлемы бесплатно", Category.MOTORBIKE),
        ("Сдаю комнату в квартире у моря, цена 4 млн", Category.ROOM),
        ("Великолепный вид, сдаю студию, цена 8 млн", Category.APARTMENT),
        ("Продам велик горный, цена 2 млн", Category.BICYCLE),
        (
            "Rental 13,000,000 VND/month APARTMENT FOR RENT, free motorbike parking",
            Category.APARTMENT,
        ),
    ],
)
def test_the_first_named_subject_is_the_listing_category(text: str, category: Category) -> None:
    result = gate(text, category_hints=vocabulary.category_hints)

    assert result.categories[0] is category, "категорию карточки даёт первый элемент"
    assert category_of(text) is category, "отбор выдачи обязан читать лот так же"


def test_client_queries_keep_rule_order_without_adjective_false_hits() -> None:
    """Запрос клиента — одна фраза, заголовка и обстановки у него нет; порядок
    правил прежний. Но прилагательные больше не выдают себя за предмет."""
    assert detect_category("1-комнатная квартира") is Category.APARTMENT
    assert detect_category("сниму комнату") is Category.ROOM
    assert detect_category("великолепный вид") is None
    assert detect_category("куплю велик") is Category.BICYCLE


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Актуально, едем домой продаю байк жены", Category.MOTORBIKE),
        ("Аренда дома в Nha Trang, 3 спальни", Category.HOUSE),
        ("Конг\nТрёхэтажный дом в аренду, 3 спальни, 270m2", Category.HOUSE),
        ("#Нячанг #аренда #сдам\nНОВЫЙ ДОМ В АРЕНДУ, 3 спальни, парковка для байка", Category.HOUSE),
        ("🏠 1️⃣➖🅱️\n📍 Рядом с центром 📐 45 м² | парковка для авто", Category.APARTMENT),
        ("Marina Suites\n2 спальни, вид на море, парковка для байка", Category.APARTMENT),
        ("ОСВОБОДИЛИСЬ 2 КОМНАТЫ по 45 м² В ЧАСТНОМ ДОМЕ", Category.HOUSE),
    ],
)
def test_body_mentions_do_not_override_the_subject(text: str, category: Category) -> None:
    """Живые формы из сухого прогона по 6691 карточке прода (18.09.2026)."""
    assert vocabulary.category_hints(text)[0] is category


def listing(listing_id: int, category: str, title: str, summary: str) -> Listing:
    return Listing(
        raw_message_id=None,
        deal_type="sell",
        category=category,
        city="nha_trang",
        title=title,
        summary=summary,
        tg_link=f"https://t.me/c/1/{listing_id}",
        posted_at=datetime(2026, 9, 18, tzinfo=UTC),
        id=listing_id,
    )


def test_a_misfiled_apartment_is_corrected_with_side_and_attributes() -> None:
    card = listing(
        1,
        "motorbike",
        "СДАЁТСЯ КВАРТИРА С 2 СПАЛЬНЯМИ",
        "Сдаётся квартира с 2 спальнями, рядом Honda Nha Trang, 12 млн в месяц",
    )

    fix = corrected(card)

    assert fix is not None
    category, deal_type, attributes = fix
    assert (category, deal_type) == ("apartment", "rent_out")
    assert attributes.get("rooms") == 2
    assert "transmission" not in attributes


def test_a_correct_card_is_left_alone() -> None:
    assert corrected(listing(2, "motorbike", "Продам Honda Lead", "Продам Honda Lead 2019")) is None


async def test_recategorize_walks_all_pages_once_and_stops() -> None:
    pages = [
        [listing(1, "motorbike", "Сдаётся квартира", "Сдаётся квартира у моря, 9 млн")],
        [listing(5, "motorbike", "Продам Lead", "Продам Honda Lead 2019")],
        [],
    ]
    asked: list[int] = []
    applied: list[tuple[int, str, str]] = []

    async def page(after_id: int, limit: int) -> list[Listing]:
        asked.append(after_id)
        return pages.pop(0)

    async def apply(listing_id: int, category: str, deal_type: str, attrs: object) -> None:
        applied.append((listing_id, category, deal_type))

    job = Recategorize(page=page, apply=apply, size=1)
    while await job.tick():
        pass

    assert asked == [0, 1, 5]
    assert applied == [(1, "apartment", "rent_out")]
    assert await job.tick() == 0, "проход один на старт процесса"
