"""Правка паспорта по реестру полей: набор изменений → ОДНА новая версия.

Чистая функция без ввода-вывода: проверка и применение изменений живут в домене, а
запись версии и проверку `base_version` делает `db/repositories/passport_edit.py`. Тем же
сервисом позже пользуется Mini App (слой `webapp → domain, db`), поэтому здесь нет ни слова
про Telegram.

Правка не трогает `raw_query`: это формулировка человека, а не критерии, и подпись версии
не должна врать, будто он что-то написал. Операция «снять поле» — то, чего не умел
`merge_edit`: тот лишь добавлял и заменял, и ни одно условие снять было нельзя.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sniffer.domain.field_spec import FieldSpec, Kind, spec_by_key, specs_for
from sniffer.domain.passport import Budget, Passport

EVENT_KIND = "manual_edit"
REMOVE = object()  # значение «снять поле»: None и пустая строка бывают честными значениями


class EditError(ValueError):
    """Правка отклонена: поле чужое для категории, значение не по виду поля, снимать нельзя."""


@dataclass(frozen=True, slots=True)
class Change:
    key: str
    value: Any = REMOVE

    @property
    def removes(self) -> bool:
        return self.value is REMOVE


def apply_changes(passport: Passport, changes: Sequence[Change]) -> tuple[Passport, dict[str, Any]]:
    """Новый паспорт и тело события `manual_edit`: `{"set": {...}, "removed": [...]}`."""
    if not changes:
        raise EditError("нечего менять")
    allowed = {spec.key for spec in specs_for(passport.category)}
    updated = passport
    done: dict[str, Any] = {}
    removed: list[str] = []
    for change in changes:
        spec = spec_by_key(change.key)
        if spec is None or spec.key not in allowed:
            raise EditError(f"поля «{change.key}» нет у этого поиска")
        if change.removes:
            if spec.required:
                raise EditError(f"«{spec.label}» нельзя снять: без него слежение не работает")
            updated = _store(updated, spec, None)
            removed.append(spec.key)
        else:
            value = _check(spec, change.value)
            updated = _store(updated, spec, value)
            done[spec.key] = value
    return updated, {"set": done, "removed": removed}


def read(passport: Passport, spec: FieldSpec) -> Any:
    """Значение поля или `None`, если не задано."""
    head, _, tail = spec.path.partition(".")
    if head == "attributes":
        return passport.attributes.get(tail)
    if head == "budget":
        return getattr(passport.budget, tail)
    value = getattr(passport, head)
    return value or None


def _check(spec: FieldSpec, value: Any) -> Any:
    if spec.kind is Kind.CHOICE:
        if value not in {option for option, _ in spec.options}:
            raise EditError(f"«{spec.label}»: такого варианта нет")
        return value
    if spec.kind is Kind.FLAG:
        if not isinstance(value, bool):
            raise EditError(f"«{spec.label}»: нужно «да» или «нет»")
        return value
    if spec.kind is Kind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise EditError(f"«{spec.label}»: нужно положительное число")
        return value
    if spec.kind is Kind.LIST:
        words = [w.strip() for w in value] if isinstance(value, list) else []
        if not words or not all(isinstance(w, str) and w for w in words):
            raise EditError(f"«{spec.label}»: нужен список слов")
        return words
    if not isinstance(value, str) or not value.strip():
        raise EditError(f"«{spec.label}»: нужен текст")
    return value.strip()


def _store(passport: Passport, spec: FieldSpec, value: Any) -> Passport:
    head, _, tail = spec.path.partition(".")
    if head == "attributes":
        attributes = dict(passport.attributes)
        for name in (tail, *spec.companions):
            attributes.pop(name, None)
        if value is not None:
            attributes[tail] = value
        return passport.model_copy(update={"attributes": attributes})
    if head == "budget":
        budget: Budget = passport.budget.model_copy(update={tail: value})
        return passport.model_copy(update={"budget": budget})
    empty: Any = [] if spec.kind is Kind.LIST else None
    return passport.model_copy(update={head: empty if value is None else value})
