"""Источник вместо сети: что поиск отдал бы по этому паспорту.

Путь от находки до карточки повторяет `bot.conversation.find_live` минус
ввод-вывод: источник → `rank_items` → карточки. Настоящим здесь остаётся именно
отбор — подделан только выход наружу. Появится в боевом пути новый шаг
(отсечение, второй проход, проверка живости), он обязан появиться и здесь:
иначе харнес меряет дорогу, по которой клиент не ходит, и его зелёный ничего не
обещает.

Курс тоже подделан и зафиксирован: живой `usd_vnd_rate()` ходит в сеть, а
отчёт, который меняется от чужого курса, не с чем сравнить.
"""

from __future__ import annotations

from sniffer.bot.conversation import Found
from sniffer.domain.passport import Passport
from sniffer.search.relevance import rank_items
from sniffer.simulation.catalog import CATALOG, Lot
from sniffer.simulation.fit import USD_VND

# Лоты, которые источник вытаскивает мимо запроса. Шум не выдуман: все лежат в
# `telegram_groups`, а поиск по чату идёт словами — «продам», «Нячанг», «срочно»
# стоят в объявлении о чём угодно, и структурных полей у чата нет НИКАКИХ.
# Отдай подделка только свою категорию — метрика «показал не то» не смогла бы
# отличиться от нуля никогда.
#
# Две оси шума, и вторую добавил realcheck 03.09.2026. Первая — чужая КАТЕГОРИЯ
# (велосипед, комната): её ловит `rank_items` разбором текста. Вторая — чужие
# МАРКА и КОРОБКА той же категории: на «ямаха» чат нёс Honda и Kymco, на
# «автомат» — механику. Структурная доска отсекла бы их полем (`_source_accepts`
# ниже так и делает), но у чата поля нет — отсев по тексту в `rank_items`
# единственная защита. Пока этих лотов в шуме не было, синтетика её и не мерила:
# `_source_accepts` вычищала чужую марку до отбора, и дыра жила незамеченной,
# пока её не вскрыл realcheck, а не симуляция.
_NOISE_KEYS = (
    "giant-escape-3",
    "room-shared-kitchen",
    "kymco-like-chat",
    "yamaha-r15-chat",
)


def lots_by_url(catalog: tuple[Lot, ...] = CATALOG) -> dict[str, Lot]:
    """Обратный ход от показанной карточки к правде о лоте."""
    return {lot.item.url: lot for lot in catalog}


def offered(passport: Passport, catalog: tuple[Lot, ...] = CATALOG) -> list[Lot]:
    """Что источник отдал бы по этому паспорту — вместе с шумом."""
    if passport.category is None:
        return list(catalog)
    matched = [
        lot
        for lot in catalog
        if lot.category is passport.category and _source_accepts(lot, passport)
    ]
    noise = [lot for lot in catalog if lot.item.external_id in _NOISE_KEYS and lot not in matched]
    return matched + noise


def _source_accepts(lot: Lot, passport: Passport) -> bool:
    """Имитировать структурные поля доски и текстовый запрос к чату.

    Это не ранжирование: источник до общего контура уже применяет явно
    переданные марку, коробку и объём. Раньше подделка игнорировала params и
    приписывала боту ошибки, которых реальный адаптер не возвращает.
    """
    attrs = passport.attributes
    if attrs.get("brand") and lot.brand.casefold() != str(attrs["brand"]).casefold():
        return False
    if attrs.get("model") and lot.model.casefold() != str(attrs["model"]).casefold():
        return False
    if attrs.get("transmission") and lot.transmission != attrs["transmission"]:
        return False
    wanted_cc = attrs.get("engine_cc")
    if wanted_cc is not None and lot.engine_cc is not None:
        target = float(str(wanted_cc))
        if abs(lot.engine_cc - target) > target * 0.25:
            return False
    return True


async def market_finder(passport: Passport) -> Found:
    """Подмена `find_live`: источник и курс подделаны, отбор — настоящий."""
    items = rank_items(passport, [lot.item for lot in offered(passport)], usd_vnd=USD_VND)
    return Found(items=items, sources=("simulated_market",))
