"""Удобства жилья из текста: есть, нет или не сказано.

Каждый случай — реальный шаблон из чатов (16 600 постов жилья, 03.10.2026), сокращённый
до строк, на которых он держится. Три исхода: `True`, `False` и отсутствие ключа — и
путать «нет» с молчанием нельзя: клиент, которому нужен лифт, не должен терять лот, где
про лифт не написано, но обязан не получить лот «без лифта».
"""

from __future__ import annotations

import pytest

from sniffer.domain.facts_amenities import amenity_facts
from sniffer.domain.facts_text import fact_text


def facts(text: str) -> dict[str, object]:
    return amenity_facts(fact_text(text))


def case(name: str, text: str, key: str, expected: object) -> object:
    return pytest.param(text, key, expected, id=name)


COMPLEX = "На территории ЖК есть бассейн, спортзал, детский садик"
PRESENT = [
    case("balcony_word", "Удобства: Балкон, стиральная машина, full мебель", "balcony", True),
    case("balcony_vi", "Ban công rộng, thoáng", "balcony", True),
    case("balcony_in_a_headline", "СДАЮТСЯ АПАРТАМЕНТЫ С БАЛКОНОМ", "balcony", True),
    case("elevator_icon_line", "🛗 Лифт\n🛡 Охрана", "elevator", True),
    case("elevator_and_stairs", "Доступ: лифт и лестница, 4-й этаж", "elevator", True),
    case("elevator_vi", "Lối đi: Thang máy, thang bộ", "elevator", True),
    case("pool_in_complex", COMPLEX, "pool", True),
    case("gym_in_complex", COMPLEX, "gym", True),
    case("pool_english", "Premium amenities: Swimming pool, gym, casino", "pool", True),
    case("pool_free", "Бассейн: бесплатно", "pool", True),
    case("washer_private", "🧺 Private washer & dryer", "washing_machine", True),
    case("washer_shared_still_a_washer", "🧺 Общая стиральная машина", "washing_machine", True),
    case("washer_vi", "Tivi, tủ lạnh, máy giặt", "washing_machine", True),
    case("aircon_plural", "кондиционеры, стиральная машинка, ТВ", "air_conditioner", True),
    case("aircon_counted", "💘 2 кондиционера 🥶", "air_conditioner", True),
    case("aircon_vi", "Full nội thất: 2 máy lạnh", "air_conditioner", True),
    case("aircon_slash", "Fully furnished, A/C, balcony", "air_conditioner", True),
]
ABSENT = [
    case("no_balcony_clause", "Студия 30 м², без балкона, Phuoc Hai", "balcony", False),
    case("no_balcony_caps", "СДАЕТСЯ КВАРТИРА БЕЗ БАЛКОНА", "balcony", False),
    case("no_balcony_after", "1 спальня + кухня; балкона нет, но есть окна", "balcony", False),
    case("no_balcony_vi", "Căn hộ 50m² | Không ban công", "balcony", False),
    case("no_balcony_ko", "CHO THUÊ CĂN HỘ KO BAN CÔNG", "balcony", False),
    case("no_balcony_nowindow", "нет окон, нет балкона.", "balcony", False),
    case("no_elevator", "🏢 2 этаж – без лифта", "elevator", False),
    case("no_elevator_english", "• No elevator\n• Private washing machine", "elevator", False),
    case("elevator_colon_no", "· Этаж: 2-й\n· Лифт: нет, лестница", "elevator", False),
    case("no_washer", "• Без стиральной машины", "washing_machine", False),
    case("no_aircon_vi", "nội thất cơ bản( không có máy lạnh, sofa)", "air_conditioner", False),
]
LISTS = [
    case(
        "list_aircon",
        "Частично меблирован (без кондиционера, стиральной машины, ТВ)",
        "air_conditioner",
        False,
    ),
    case(
        "list_washer",
        "Частично меблирован (без кондиционера, стиральной машины, ТВ)",
        "washing_machine",
        False,
    ),
    case("list_leaves_others_alone", "Без балкона и лифта, есть бассейн", "pool", True),
    case("without_commission_lift", "Без комиссии, лифт, бассейн", "elevator", True),
    case("without_commission_pool", "Без комиссии, лифт, бассейн", "pool", True),
]
NOT_NEGATIONS = [
    case(
        "lift_not_used",
        "На первом этаже — доступ без использования лифта или лестницы",
        "elevator",
        None,
    ),
    case("lift_not_waited_for", "Доступ без ступеней и ожидания лифта", "elevator", None),
    case(
        "balcony_not_counted", "Просторная комната около 27 м² (без учёта балкона)", "balcony", None
    ),
    case("lifts_are_fine", "3 этаж, с лифтами нет проблем, на окнах решетки", "elevator", True),
    case(
        "one_room_no_aircon",
        "- В гостиной нет кондиционера, владелец купил вентилятор",
        "air_conditioner",
        None,
    ),
    case(
        "a_service_is_not_a_machine",
        "🧺 Зона для стирки\nУборка 3 раза в неделю",
        "washing_machine",
        None,
    ),
]
NEARBY = [
    case("pool_nearby", "📍 Локация:\n• Запад Нячанга\n• Рядом: бассейн, парк", "pool", None),
    case("gym_around", "Вокруг: супермаркет, кафе, рестораны, спортзал и другое", "gym", None),
    case("gym_near_a_dotted_name", "Рядом с Co.opmart, кафе, спортзалами и спа", "gym", None),
    case("the_next_sentence_counts", "Рядом пляж. Есть бассейн и лифт.", "pool", True),
]
CONFLICTS = [
    case("with_and_without", "Доступны варианты с балконом и без балкона", "balcony", None),
    case("no_elevator_but_a_footer_lift", "Без лифта.\n\nУдобства: лифт", "elevator", None),
]


@pytest.mark.parametrize(
    ("text", "key", "expected"), PRESENT + ABSENT + LISTS + NOT_NEGATIONS + NEARBY + CONFLICTS
)
def test_an_amenity_is_there_not_there_or_not_said(text: str, key: str, expected: object) -> None:
    assert facts(text).get(key) is expected


def pet(name: str, text: str, expected: bool | None) -> object:
    return pytest.param(text, expected, id=name)


PETS = [
    pet("allowed", "🐶🐱 Можно с домашними животными", True),
    pet("allowed_short", "✅ МОЖНО С ЖИВОТНЫМИ", True),
    pet("allowed_with_a_fee", "С животными: можно, доплата 200 000 VND за питомца", True),
    pet("allowed_english", "🐾 Pet-friendly (additional fee)", True),
    pet("allowed_okay", "👉🏻 Pets okay", True),
    pet("allowed_discussed", "Проживание с питомцами обсуждается (доплата 500 000 VND)", True),
    pet("allowed_vi", "🐾 Cho nuôi pet\n💰 Giá thuê: 14.000.000đ/tháng", True),
    pet("a_fee_line_means_allowed", "🐶 Домашние животные: 500 000 донгов/месяц", True),
    pet("banned", "• Без животных", False),
    pet("banned_cannot", "❌ С животными нельзя", False),
    pet("banned_not_allowed", "Домашние животные: не разрешены", False),
    pet("banned_english", "🚫no pet, no electronic bike", False),
    pet("banned_vi", "Không thú cưng", False),
    pet("banned_ko", "ko pet ko xe điện", False),
    pet("tariff_is_not_a_ban", "Цена: 8 млн — без животных\n🐶 С питомцами: +500 000 VND", True),
    pet(
        "small_allowed_heavy_not",
        "Разрешены небольшие животные; животные тяжелее 7 кг не допускаются",
        None,
    ),
    pet("the_menu_item_is_not_a_permission", "👉 АРЕНДА С ЖИВОТНЫМИ", None),
    pet("silence", "Студия 30 м², балкон", None),
]


@pytest.mark.parametrize(("text", "expected"), PETS)
def test_pets_are_allowed_banned_or_not_said(text: str, expected: bool | None) -> None:
    assert facts(text).get("pets_allowed") is expected


def test_a_ban_in_one_clause_does_not_cancel_a_permission_in_another() -> None:
    """«Можно с животными, курение запрещено» — две мысли, и запрет относится ко второй."""
    assert facts("Можно с животными, курение запрещено").get("pets_allowed") is True


def kitchen(name: str, text: str, expected: str | None) -> object:
    return pytest.param(text, expected, id=name)


KITCHEN = [
    kitchen("separate", "🍳 Отдельная кухня и общая прачечная", "separate"),
    kitchen("separate_english", "a separate kitchen, living room", "separate"),
    kitchen("shared", "🍳 Общая кухня — проживание с двумя соседями", "shared"),
    kitchen("shared_vi", "Bếp chung, phòng tắm riêng", "shared"),
    kitchen("kitchen_living_room_is_neither", "кухня-гостиная, 2 спальни", None),
    kitchen("both_is_a_conflict", "Отдельная кухня\nОбщая кухня", None),
]


@pytest.mark.parametrize(("text", "expected"), KITCHEN)
def test_a_kitchen_is_separate_shared_or_not_named(text: str, expected: str | None) -> None:
    assert facts(text).get("kitchen") == expected


def test_a_silent_post_names_nothing() -> None:
    """Молчание — не «нет»: ключей в ответе нет вовсе, а не `False`."""
    assert facts("Сдаётся квартира у моря, 10 млн") == {}


def test_a_dishwasher_is_not_a_washing_machine() -> None:
    """«Dishwasher» содержит «washer»: посудомойка стиральной машиной не становится."""
    assert "washing_machine" not in facts("Kitchen: dishwasher, oven, fridge")
