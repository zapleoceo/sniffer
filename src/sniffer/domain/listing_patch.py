"""Патч карточки: что проход догона вправе в ней изменить.

Карточку, созданную прежним правилом, можно пересчитать из исходного текста
объявления: цена, район, заголовок, язык, свойства. Вывод из текста — чистая
функция, она ничего не пишет, а возвращает патч; запись — отдельный шаг в `db/`.
Патч — это договор между ними, и записан он здесь, в `domain/`, потому что нужен
обеим сторонам: слой `pipeline` его строит, слой `db` по нему пишет, а знать
друг о друге им нельзя (`tests/test_layers.py`).

**Что проход менять вправе, перечислено списком, а не угадывается.** Цена, район,
заголовок и язык — производные от текста: пересчитал и получил то же самое.
Остальное — жизнь и личность карточки (`is_active`, `screened_at`, `posted_at`,
`deal_type`, `category`, `city`): их решают воронка и проверка моделью, а
пересчёт по тексту перезаписал бы чужое решение. Патч с чужой колонкой не
создаётся вовсе — это ошибка на этапе построения, а не на записи.

**Атрибуты не колонка, а слияние.** Они пишутся как `attributes || патч`:
ключи патча главнее, остальные целы. Так проход не теряет то, что извлечено
другим способом (марка и объём — разбором запроса, модель — проверкой).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from sniffer.domain.records import Listing

PATCHABLE_COLUMNS: frozenset[str] = frozenset(
    {"price_amount", "price_currency", "price_period", "district", "title", "lang"}
)


class PatchError(ValueError):
    """Патч просит изменить то, что проход менять не вправе."""


class PatchClash(ValueError):
    """Два вывода претендуют на одну колонку или на один ключ атрибутов."""


@dataclass(frozen=True, slots=True)
class ListingPatch:
    """Только реальные изменения: то, что уже так, в патч не попадает.

    Поэтому повторный проход пуст — `is_empty` — и в базу не пишет вовсе.
    `outcomes` — что произошло с карточкой, для отчёта прохода: у вывода
    «цена» это `price.filled`, `price.same`… Пустой патч исходы нести может
    («цена уже верна» — тоже исход).
    """

    columns: Mapping[str, Any] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    outcomes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        foreign = sorted(set(self.columns) - PATCHABLE_COLUMNS)
        if foreign:
            raise PatchError(
                f"проход не вправе менять {', '.join(foreign)}: "
                "атрибуты передаются отдельно, остальное решают воронка и проверка модели"
            )

    @property
    def is_empty(self) -> bool:
        return not self.columns and not self.attributes

    def merged(self, other: ListingPatch) -> ListingPatch:
        """Объединение двух выводов; общая колонка или общий ключ — ошибка.

        Совпавшее значение не оправдание: два вывода, претендующих на одно
        поле, — это нарушенная граница между ними, а не удача.
        """
        shared = sorted(
            (set(self.columns) & set(other.columns))
            | (set(self.attributes) & set(other.attributes))
        )
        if shared:
            raise PatchClash(f"два вывода претендуют на одно: {', '.join(shared)}")
        return ListingPatch(
            {**self.columns, **other.columns},
            {**self.attributes, **other.attributes},
            self.outcomes + other.outcomes,
        )

    def applied_to(self, listing: Listing) -> Listing:
        """Карточка после записи патча — как это сделает база, но в памяти.

        Спецификация для SQL: слияние атрибутов здесь и `attributes || патч`
        там обязаны давать одно и то же, и живой тест репозитория сверяет их.
        """
        return replace(
            listing, **self.columns, attributes={**listing.attributes, **self.attributes}
        )


@dataclass(frozen=True, slots=True)
class ListingWithText:
    """Карточка и текст, из которого её пересчитывают.

    `text` — `None`, когда сырья нет: тогда пересчитывать нечего, и проход
    говорит об этом в отчёте, а не молчит.
    """

    listing: Listing
    text: str | None


class WriteStatus(StrEnum):
    APPLIED = "applied"
    # Строка изменилась с момента чтения: решение принято по устаревшим данным,
    # поэтому не записано. Повторный проход подхватит — он идемпотентен.
    STALE = "stale"
    # База отказала на этой строке; остальные пачки это не затронуло.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Чем кончилась запись патча одной карточки.

    `error` — имена классов сбоя без текста: в тексте ошибки базы лежат
    параметры SQL, а в них — заголовки и атрибуты объявлений.
    """

    status: WriteStatus
    error: str = ""
