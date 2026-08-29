"""Слой источников: контракт адаптера и реестр.

Правило проекта — добавление источника не должно менять ни одной существующей
строки. Поэтому воронка, планировщик и бот знают только про `RawItem` и
`Source.search()`, а адаптер попадает в реестр самим фактом того, что его
модуль лежит в этом пакете.

Реестр здесь — кодовая половина того, что в spec-v2 (2.1) описано таблицей
`sources`. Таблица появится вместе с БД и будет хранить `enabled` и состояние
`degraded`; имя источника и класс адаптера всё равно живут в коде.
"""

from __future__ import annotations

import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from importlib import import_module
from typing import Any, ClassVar

_PACKAGE = "sniffer.sources"

# Источники ходят в чужие сайты, а не в наши сервисы. 15 с — это уже
# «источник лежит»: на весь план отведено 90 с (spec-v2, 2.3), и один
# медленный адаптер не имеет права съесть бюджет остальных.
DEFAULT_TIMEOUT_S = 15.0


@dataclass(slots=True)
class RawItem:
    """Находка до воронки: что источник отдал, без интерпретации.

    `price_raw` лежит рядом с `price_vnd` намеренно. Verifier проверяет
    карточку по исходному тексту (spec-v2, 3.1), и если выбросить оригинал
    цены, сверять распознанное число станет не с чем.
    """

    source: str
    external_id: str
    url: str
    title: str = ""
    text: str = ""
    price_raw: str = ""
    price_vnd: int | None = None
    posted_at: datetime | None = None
    images: list[str] = field(default_factory=list)
    seller_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Source(ABC):
    """Один источник — один метод. Всё остальное различается данными."""

    name: ClassVar[str] = ""
    enabled: ClassVar[bool] = True

    def __init__(self) -> None:
        # Источник, споткнувшийся о сеть или о чужой JSON, помечает себя и
        # исключается из следующих планов до ручной починки (spec-v2, 6.2).
        # Клиент этого не замечает — выдача собирается из остальных.
        self.degraded = False

    async def aclose(self) -> None:
        """Освободить ресурсы адаптера. По умолчанию их нет.

        Объявлено в базе, чтобы исполнитель плана закрывал источники, ничего о
        них не зная: адаптер с http-клиентом переопределит, адаптер без него
        не заметит.
        """
        return None

    @abstractmethod
    async def search(self, query: str, params: dict[str, Any]) -> list[RawItem]:
        """Контракт: наружу не бросает.

        Сеть и чужой недокументированный JSON ненадёжны ожидаемо, а не
        исключительно. Упавший адаптер обязан вернуть пустой список и
        выставить `degraded`, иначе один сломавшийся источник уронит весь
        план поиска.
        """


class UnknownSourceError(LookupError):
    """В реестре нет адаптера с таким именем."""


_ADAPTERS: dict[str, type[Source]] = {}
_discovered = False


def register(adapter: type[Source]) -> type[Source]:
    """Декоратор адаптера: `@register` над классом — вся регистрация."""
    if not adapter.name:
        raise ValueError(f"{adapter.__qualname__} без атрибута name в реестр не попадёт")
    _ADAPTERS[adapter.name] = adapter
    return adapter


def get_source(name: str, **kwargs: Any) -> Source:
    """Экземпляр адаптера по имени из плана поиска."""
    _discover()
    try:
        adapter = _ADAPTERS[name]
    except KeyError as exc:
        raise UnknownSourceError(f"источник {name!r} не зарегистрирован") from exc
    return adapter(**kwargs)


def registered_sources(*, enabled_only: bool = True) -> dict[str, type[Source]]:
    """Что планировщик вправе поставить в план."""
    _discover()
    return {
        name: adapter for name, adapter in _ADAPTERS.items() if adapter.enabled or not enabled_only
    }


def _discover() -> None:
    """Импортирует модули пакета — иначе декораторы не отработают.

    Явный список импортов пришлось бы править при каждом новом источнике,
    а это ровно то, что запрещено правилом выше.
    """
    global _discovered
    if _discovered:
        return
    package = import_module(_PACKAGE)
    for module in pkgutil.iter_modules(package.__path__):
        if module.name != "base" and not module.name.startswith("_"):
            import_module(f"{_PACKAGE}.{module.name}")
    _discovered = True
