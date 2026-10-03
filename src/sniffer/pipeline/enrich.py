"""Реестр выводов из текста: один проход, любое число выводов.

Карточку можно пересчитать из исходного текста: цена, район, площадь, этаж,
заголовок, язык. Каждое такое знание — отдельный **вывод** (`Derivation`): он
получает карточку и текст, а возвращает патч (`ListingPatch`) — что в карточке
стоит изменить, и ничего не пишет. Выводы собираются в одном месте
(`DERIVATIONS`), а `derive()` прогоняет их и сливает патчи. Запись патча в базу
— не здесь, а в `db/repositories/listing_enrichment.py`; обход карточек — в
`worker/enrich.py`. Поэтому тот же реестр годится для нового сообщения (воронка
может звать `derive` на лету) и для накопленного (проход догона).

**Как добавить вывод.** Класс с `name` и `derive(listing, text)`, одна строка в
`DERIVATIONS`. Ни воронка, ни воркер, ни репозиторий, ни отчёт не меняются:
исходы называются по выводу (`price.filled`, `district.filled`) и попадают в
отчёт сами. Договор вывода держит `tests/test_enrich.py` — он берёт реестр, а не
свой список, поэтому новый вывод проверяется без правки теста (образцы
добавляются в `SAMPLES`):

* у вывода есть хотя бы один исход, и назван он по выводу;
* патч — только разница: повторный проход по результату пуст;
* колонки — из `PATCHABLE_COLUMNS`, иначе патч не создаётся вовсе;
* два вывода не претендуют на одну колонку или один ключ атрибутов.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.records import Listing
from sniffer.pipeline.enrich_price import PriceDerivation


class Derivation(Protocol):
    """Один вывод из текста: что в карточке можно узнать заново.

    `name` — пространство исходов вывода: все его исходы начинаются с
    `name + "."`, и отчёт прохода группирует по ним.
    """

    name: str

    def derive(self, listing: Listing, text: str) -> ListingPatch: ...


class DerivationFailed(Exception):
    """Вывод не справился с этой карточкой. Текст чужой ошибки сюда не переносится.

    Имя вывода — в `derivation`, причина — в `__cause__`: в тексте исключения
    разбора бывает кусок объявления с телефоном, а сообщение уходит в лог.
    """

    def __init__(self, derivation: str) -> None:
        super().__init__(f"вывод «{derivation}» не справился")
        self.derivation = derivation


# Единственное место, где перечислены выводы. Порядок не важен: патчи сливаются
# без приоритетов, а спор за одно поле — ошибка (`PatchClash`), а не очерёдность.
DERIVATIONS: tuple[Derivation, ...] = (PriceDerivation(),)


def derive(
    listing: Listing, raw_text: str, *, derivations: Sequence[Derivation] | None = None
) -> ListingPatch:
    """Патч карточки по исходному тексту: все выводы реестра, слитые в один.

    `derivations` по умолчанию — реестр на момент вызова, а не на момент
    импорта: добавленный в `DERIVATIONS` вывод подхватывается сразу.
    """
    patch = ListingPatch()
    for derivation in DERIVATIONS if derivations is None else derivations:
        try:
            patch = patch.merged(derivation.derive(listing, raw_text))
        except Exception as err:
            # Любая причина — разобранный чужой текст, спор за поле, баг вывода —
            # для прохода одно: эту карточку пропустить и сказать, чей вывод.
            raise DerivationFailed(derivation.name) from err
    return patch
