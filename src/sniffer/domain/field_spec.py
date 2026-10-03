"""Реестр полей паспорта для редактора: что человек может поправить и что из этого видит монитор.

Один реестр на знание «какие бывают условия поиска». По нему рисуются чипы карточки
фильтра (`bot/filter_card.py`), по нему же `passport_edit` проверяет правку. Список
атрибутов по категориям живёт в `CATEGORY_ATTRIBUTES`; здесь он дополнен тем, чего там
нет и быть не должно: подписью, видом значения и признаком `monitor`. Согласованность
двух списков держит `tests/test_field_spec.py`: атрибут без поля в реестре — условие,
которое нельзя ни показать, ни снять.

`monitor` — правда о том, как поле читает монитор слежения (`matching/rules.py`, таблица в
R6 §3.6): `yes` — сужает, `partial` — только равенство и только если у лота значение
известно, `no` — не читает вовсе. Редактор обязан говорить это человеку: правка района и
срока без эффекта хуже, чем отсутствие правки.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sniffer.domain.passport import CATEGORY_ATTRIBUTES, Category


class Monitor(StrEnum):
    YES = "yes"
    PARTIAL = "partial"
    NO = "no"


class Kind(StrEnum):
    CHOICE = "choice"  # одно из `options`
    FLAG = "flag"  # да / нет
    NUMBER = "number"
    TEXT = "text"
    LIST = "list"  # список слов


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str  # провод: ≤ 12 знаков, влезает в callback_data рядом с корнем и версией
    path: str  # `city`, `budget.max`, `attributes.transmission`, `districts`
    label: str
    kind: Kind
    monitor: Monitor
    options: tuple[tuple[str, str], ...] = ()
    categories: tuple[Category, ...] | None = None  # None — для любой категории
    required: bool = False  # без города монитор не работает вовсе: снять нельзя
    companions: tuple[str, ...] = ()  # атрибуты, что снимаются вместе с полем

    @property
    def attribute(self) -> str | None:
        head, _, tail = self.path.partition(".")
        return tail if head == "attributes" else None


def _owners(key: str) -> tuple[Category, ...]:
    """Категории, у которых этот атрибут есть в паспорте: одно знание, а не второй список."""
    return tuple(c for c, names in CATEGORY_ATTRIBUTES.items() if key in names)


def _flag(key: str, label: str) -> FieldSpec:
    return FieldSpec(
        key, f"attributes.{key}", label, Kind.FLAG, Monitor.PARTIAL, categories=_owners(key)
    )


def _number(key: str, label: str) -> FieldSpec:
    return FieldSpec(
        key, f"attributes.{key}", label, Kind.NUMBER, Monitor.PARTIAL, categories=_owners(key)
    )


_BIKE = (Category.MOTORBIKE,)

REGISTRY: tuple[FieldSpec, ...] = (
    FieldSpec("city", "city", "Город", Kind.TEXT, Monitor.YES, required=True),
    FieldSpec("budget_max", "budget.max", "Бюджет до", Kind.NUMBER, Monitor.PARTIAL),
    FieldSpec("budget_min", "budget.min", "Бюджет от", Kind.NUMBER, Monitor.NO),
    FieldSpec("districts", "districts", "Районы", Kind.LIST, Monitor.NO),
    FieldSpec("must_have", "must_have", "Обязательно", Kind.LIST, Monitor.NO),
    FieldSpec("deal_breakers", "deal_breakers", "Исключить", Kind.LIST, Monitor.NO),
    FieldSpec(
        "transmission",
        "attributes.transmission",
        "Коробка",
        Kind.CHOICE,
        Monitor.PARTIAL,
        (("automatic", "автомат"), ("manual", "механика"), ("semi", "полуавтомат")),
        _BIKE,
    ),
    FieldSpec("brand", "attributes.brand", "Марка", Kind.TEXT, Monitor.PARTIAL, categories=_BIKE),
    FieldSpec("model", "attributes.model", "Модель", Kind.TEXT, Monitor.YES, categories=_BIKE),
    FieldSpec(
        "engine_cc",
        "attributes.engine_cc",
        "Объём, см³",
        Kind.NUMBER,
        Monitor.PARTIAL,
        categories=_BIKE,
        companions=("engine_cc_dir",),
    ),
    _number("year_min", "Год от"),
    FieldSpec(
        "condition",
        "attributes.condition",
        "Состояние",
        Kind.CHOICE,
        Monitor.PARTIAL,
        (("new", "новый"), ("good", "хорошее"), ("worn", "б/у, видно износ")),
        _BIKE,
    ),
    FieldSpec(
        "papers",
        "attributes.papers",
        "Документы",
        Kind.CHOICE,
        Monitor.PARTIAL,
        (("blue_card", "блюкарта"), ("none", "без документов")),
        _BIKE,
    ),
    _flag("delivery", "Доставка"),
    _flag("test_ride", "Тест-драйв"),
    FieldSpec(
        "power",
        "attributes.power",
        "Двигатель",
        Kind.CHOICE,
        Monitor.YES,
        (("fuel", "бензин"), ("electric", "электро")),
        _BIKE,
    ),
    _number("rooms", "Комнат"),
    _number("area_m2", "Площадь, м²"),
    _number("floor", "Этаж"),
    _number("floors_total", "Этажей в доме"),
    *(
        _flag(key, label)
        for key, label in (
            ("furnished", "С мебелью"),
            ("air_conditioner", "Кондиционер"),
            ("washing_machine", "Стиральная машина"),
            ("pool", "Бассейн"),
            ("gym", "Спортзал"),
            ("elevator", "Лифт"),
            ("balcony", "Балкон"),
            ("sea_view", "Вид на море"),
            ("pets_allowed", "Можно с животными"),
            ("utilities_included", "Коммуналка включена"),
        )
    ),
    FieldSpec(
        "shared",
        "attributes.shared",
        "Общее",
        Kind.CHOICE,
        Monitor.PARTIAL,
        (("bathroom", "санузел"), ("kitchen", "кухня")),
        (Category.ROOM,),
    ),
    _number("min_term_months", "Срок от, мес."),
    _number("deposit_months", "Залог, мес."),
)

_BY_KEY = {spec.key: spec for spec in REGISTRY}
# Дубль ключа молча перекрыл бы поле при поиске по ключу — падаем при импорте.
assert len(_BY_KEY) == len(REGISTRY), "в реестре полей два поля с одним ключом"


def spec_by_key(key: str) -> FieldSpec | None:
    return _BY_KEY.get(key)


def specs_for(category: Category | None) -> list[FieldSpec]:
    """Поля, имеющие смысл для категории, в порядке реестра."""
    return [s for s in REGISTRY if s.categories is None or (category in s.categories)]


def uncovered_attributes() -> set[tuple[Category, str]]:
    """Атрибуты `CATEGORY_ATTRIBUTES`, которым в реестре не нашлось поля (должно быть пусто)."""
    covered = {(c, s.attribute) for s in REGISTRY for c in s.categories or () if s.attribute}
    return {
        (category, name)
        for category, names in CATEGORY_ATTRIBUTES.items()
        for name in names
        if (category, name) not in covered and name not in _companions()
    }


def _companions() -> set[str]:
    return {name for spec in REGISTRY for name in spec.companions}
