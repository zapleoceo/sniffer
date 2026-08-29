"""Справочник Chotot: всё, что снято с API опытным путём.

Вынесено из адаптера отдельно, потому что это не код, а наблюдения. API
недокументирован (spec-v2, 6.2), у Chotot нет ни списка регионов, ни списка
категорий: `static.chotot.com/storage/chotot-parameters/regions_v2.json` и
`gateway.chotot.com/v1/public/chotot-regions` отдают 404. Значения ниже сняты
живыми запросами 2026-08-29 и протухнут молча — искать их надо здесь, а не по
всему адаптеру.

Как перепроверить: запрос без фильтра по региону, затем пары
(`region_v2`, `region_name`) из поля каждого объявления.
"""

from __future__ import annotations

from sniffer.domain.passport import Category

SOURCE_NAME = "chotot"

GATEWAY_URL = "https://gateway.chotot.com/v1/public/ad-listing"
# Короткая форма ссылки: отвечает 200 и редиректит на канонический адрес вида
# xe.chotot.com/mua-ban-xe-may-thanh-pho-nha-trang-khanh-hoa/<list_id>.htm.
# Собирать канонический самим — значит завязаться на слаги категорий.
LISTING_URL = "https://www.chotot.com/{list_id}.htm"

# Gateway отвечает и без заголовка, но недокументированный публичный эндпоинт
# не то место, где стоит выделяться из обычного браузерного трафика.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Сервер режет limit до 50 молча: запрос limit=1000 вернул 50 объявлений.
MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# Тип объявления, который шлёт сам сайт. На выдачу для cg=2020 не влияет
# (total одинаковый с ним и без него), но повторять запрос живого клиента у
# недокументированного API безопаснее, чем изобретать свой.
AD_TYPE = "s,k"

# Обратная проверка кода Кханьхоа: с region_v2=7044 все 50 объявлений первой
# страницы вернулись с region_name «Khánh Hòa». Нумерация не сплошная —
# соседние круглые числа пустые, угадать код нельзя, только снять.
REGION_V2: dict[str, int] = {
    "nha_trang": 7044,  # Khánh Hòa
    "da_nang": 3017,
    "ha_noi": 12000,
    "ho_chi_minh": 13000,
}

# Кханьхоа тянется на сотню километров: Cam Ranh (704402) и Ninh Hòa (704405)
# лежат в той же провинции, но байк оттуда клиенту в Нячанге бесполезен.
# Поэтому по умолчанию сужаем до района города.
AREA_V2: dict[str, int] = {
    "nha_trang": 704401,  # Thành phố Nha Trang
}

# cg — категория Chotot. 2020 «Xe máy» проверен живым запросом.
CATEGORY_CG: dict[Category, int] = {
    Category.MOTORBIKE: 2020,
}
