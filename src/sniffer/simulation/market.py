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

# Лоты, которые источник вытаскивает мимо запроса. Шум не выдуман: оба лежат в
# `telegram_groups`, а поиск по чату идёт словами — «продам», «Нячанг», «срочно»
# стоят в объявлении о чём угодно, и структурного поля «категория» у чата нет.
# Отдай подделка только свою категорию — метрика «показал не то» не смогла бы
# отличиться от нуля никогда.
_NOISE_KEYS = ("giant-escape-3", "room-shared-kitchen")


def lots_by_url(catalog: tuple[Lot, ...] = CATALOG) -> dict[str, Lot]:
    """Обратный ход от показанной карточки к правде о лоте."""
    return {lot.item.url: lot for lot in catalog}


def offered(passport: Passport, catalog: tuple[Lot, ...] = CATALOG) -> list[Lot]:
    """Что источник отдал бы по этому паспорту — вместе с шумом."""
    if passport.category is None:
        return list(catalog)
    matched = [lot for lot in catalog if lot.category is passport.category]
    noise = [lot for lot in catalog if lot.item.external_id in _NOISE_KEYS and lot not in matched]
    return matched + noise


async def market_finder(passport: Passport) -> Found:
    """Подмена `find_live`: источник и курс подделаны, отбор — настоящий."""
    items = rank_items(passport, [lot.item for lot in offered(passport)], usd_vnd=USD_VND)
    return Found(items=items, sources=("simulated_market",))
