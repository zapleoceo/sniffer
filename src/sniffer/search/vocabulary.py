"""Словарь рынка: какими словами торгуют, а не какими формулируют запрос.

Знание одно, потребителей два: промпт отдаёт его модели как отправную точку,
фолбэк строит из него готовый план. Продублировать его в обоих местах — значит
однажды поправить только одно.

Источник данных не догадки, а ручной поиск байка в Нячанге 14–17 августа 2026
(spec-v2, раздел 7): «скутер» не находил половину лотов, «инжектор» и
«блюкарт» находили; вьетнамцы в Telegram байки не продают вообще.
"""

from __future__ import annotations

from sniffer.domain.passport import Category, Intent

# Языки рынка Нячанга. Порядок = порядок убывания плотности объявлений.
MARKET_LANGS: tuple[str, ...] = ("ru", "vi", "en")

# На каком языке пишут продавцы конкретного источника. Русский на Chotot и
# вьетнамский в русском чате Нячанга одинаково возвращают ноль, поэтому язык
# выбирается по источнику, а не по языку клиента.
SOURCE_LANGS: dict[str, tuple[str, ...]] = {
    "telegram_groups": ("ru", "en"),
    "telegram_discover": ("ru", "en"),
    "chotot": ("vi",),
    "web": ("vi", "ru"),
    "facebook": ("vi", "en"),
}

# Существительные и жаргон предмета. Глаголы сделки живут отдельно, в
# INTENT_TERMS: одно и то же «xe ga» ищется и в продаже, и в аренде.
CATEGORY_TERMS: dict[Category, dict[str, tuple[str, ...]]] = {
    Category.MOTORBIKE: {
        "ru": ("скутер", "байк", "инжектор", "блюкарт"),
        "vi": ("xe ga", "xe máy cũ", "xe tay ga"),
        "en": ("scooter", "used motorbike"),
    },
    Category.BICYCLE: {
        "ru": ("велосипед", "велик"),
        "vi": ("xe đạp",),
        "en": ("bicycle",),
    },
    Category.CAR: {
        "ru": ("машина", "авто"),
        "vi": ("ô tô cũ", "xe hơi"),
        "en": ("used car",),
    },
    Category.APARTMENT: {
        "ru": ("квартира", "апартаменты", "студия"),
        "vi": ("căn hộ", "chung cư"),
        "en": ("apartment", "studio"),
    },
    Category.ROOM: {
        "ru": ("комната",),
        "vi": ("phòng trọ", "phòng cho thuê"),
        "en": ("room",),
    },
    Category.HOUSE: {
        "ru": ("дом", "вилла"),
        "vi": ("nhà nguyên căn", "biệt thự"),
        "en": ("house", "villa"),
    },
}

# Намерение клиента переворачивается в намерение автора объявления: клиент
# покупает — ищем тех, кто продаёт.
INTENT_TERMS: dict[Intent, dict[str, tuple[str, ...]]] = {
    Intent.BUY: {"ru": ("продам",), "vi": ("bán",), "en": ("for sale",)},
    Intent.RENT: {"ru": ("сдам",), "vi": ("cho thuê",), "en": ("for rent",)},
    Intent.SELL: {"ru": ("куплю",), "vi": ("cần mua",), "en": ("wanted",)},
    Intent.RENT_OUT: {"ru": ("сниму",), "vi": ("cần thuê",), "en": ("looking to rent",)},
}

# Города, по которым мы действительно ищем: под них собран реестр чатов и сняты
# параметры Chotot. Город в паспорте нормализован (`nha_trang`), а искать надо
# тем написанием, которым его пишут в объявлениях.
CITY_NAMES: dict[str, dict[str, str]] = {
    "nha_trang": {"ru": "Нячанг", "vi": "Nha Trang", "en": "Nha Trang"},
    "da_nang": {"ru": "Дананг", "vi": "Đà Nẵng", "en": "Da Nang"},
}

# Города, которые мы узнаём, но пока не обслуживаем. Узнавать их обязательно, и
# причина не в вежливости: неузнанный город подставляется городом по умолчанию,
# и «ищу скутер в Хойане» после «ищу скутер в Нячанге» приходит с тем же
# намерением, категорией и городом — то есть неотличимо от повтора прежней
# просьбы. Клиент получал переспрос вместо ответа.
#
# Список короткий намеренно: сюда попадают только направления, куда переезжают с
# нашего рынка. Каждое лишнее название — риск поймать город там, где его нет
# («hue» — обычное английское слово), а ложный город означает отказ искать.
OTHER_CITY_NAMES: dict[str, dict[str, str]] = {
    "hoi_an": {"ru": "Хойан", "vi": "Hội An", "en": "Hoi An"},
    "da_lat": {"ru": "Далат", "vi": "Đà Lạt", "en": "Da Lat"},
    "vung_tau": {"ru": "Вунгтау", "vi": "Vũng Tàu", "en": "Vung Tau"},
    "phan_thiet": {"ru": "Фантьет", "vi": "Phan Thiết", "en": "Phan Thiet"},
    "phu_quoc": {"ru": "Фукуок", "vi": "Phú Quốc", "en": "Phu Quoc"},
    "ha_noi": {"ru": "Ханой", "vi": "Hà Nội", "en": "Hanoi"},
    "ho_chi_minh": {"ru": "Хошимин", "vi": "Hồ Chí Minh", "en": "Ho Chi Minh"},
}

ALL_CITY_NAMES: dict[str, dict[str, str]] = {**CITY_NAMES, **OTHER_CITY_NAMES}

# Разговорные написания, которых нет ни в справочнике, ни в слаге. Отдельно от
# `ALL_CITY_NAMES`, потому что это не название на языке, а просто ещё одно слово
# для поиска: «Сайгон» и «Хошимин» — один город.
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "ho_chi_minh": ("Сайгон", "Saigon", "Sài Gòn", "HCMC"),
    "nha_trang": ("Ня Чанг",),
}


def source_langs(source: str) -> tuple[str, ...]:
    """Незнакомому источнику отдаём все языки рынка.

    Добавление источника — строка в таблице `sources`, а не правка этого файла:
    без языка в словаре новый адаптер должен работать, пусть и вслепую.
    """
    return SOURCE_LANGS.get(source, MARKET_LANGS)


def plan_langs(sources: list[str]) -> list[str]:
    """Языки, которые вообще имеют смысл для этого набора источников."""
    langs = [lang for source in sources for lang in source_langs(source)]
    return list(dict.fromkeys(langs))


def category_terms(category: Category | None, lang: str) -> tuple[str, ...]:
    if category is None:
        return ()
    return CATEGORY_TERMS.get(category, {}).get(lang, ())


def intent_terms(intent: Intent | None, lang: str) -> tuple[str, ...]:
    if intent is None:
        return ()
    return INTENT_TERMS.get(intent, {}).get(lang, ())


def city_name(city: str | None, lang: str) -> str:
    """Незнакомый город разворачиваем из слага: `quy_nhon` → `Quy Nhon`."""
    if not city:
        return ""
    known = ALL_CITY_NAMES.get(city)
    if known:
        return known.get(lang, known.get("en", city))
    return city.replace("_", " ").title()


def city_variants(slug: str) -> tuple[str, ...]:
    """Все написания города — этим ищут название в тексте клиента.

    Слаг тоже вариант: «nha trang» клиент пишет и латиницей.
    """
    names = ALL_CITY_NAMES.get(slug, {})
    return tuple(sorted({*names.values(), *CITY_ALIASES.get(slug, ()), slug.replace("_", " ")}))


def is_served(city: str | None) -> bool:
    """Ищем ли мы в этом городе.

    Пустой город — да: его подставит `default_city`, отказывать не за что.
    """
    return not city or city in CITY_NAMES


def served_cities(lang: str) -> tuple[str, ...]:
    """Названия обслуживаемых городов — для ответа «пока работаю только по …».

    Из того же словаря, что и поиск: список городов в тексте бота, набранный
    руками, разъехался бы с реальностью на первом же новом городе.
    """
    return tuple(city_name(slug, lang) for slug in CITY_NAMES)
