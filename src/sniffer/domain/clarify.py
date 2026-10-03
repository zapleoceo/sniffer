"""Что делать дальше: спросить ещё или искать. Детерминированная функция состояния.

Без модели и без ввода-вывода — потому что воспроизводимое решение можно
проверить свойствами (никогда не спрашивает заполненное, не выходит за потолок,
всегда оставляет выход), а решение модели нельзя.

Правила (r1_dialogue 6.4):
1. Подходит не больше десяти, клиент отказался сужать или вопросов уже четыре —
   искать.
2. Кандидаты — поля реестра, которых нет в паспорте и среди заданных и у
   которых «не указано» не больше 60%: спрашивать про поле, которое база не
   знает, значит гонять человека впустую.
3. Оценка — ожидаемый остаток `E = unknown + Σ n² / Σ n` (кого оставит ответ,
   если значение равновероятно по частоте); выигрыш `1 - E / total`. Побеждает
   больший; если и он меньше 25% — искать: «следующий вопрос ничего не режет»,
   а не «допрос ради допроса».
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sniffer.domain.dialogue import DialogueState
from sniffer.domain.facets import OPEN_END, TARGET_SIZE, Facet, FacetReport
from sniffer.domain.fields import SPECS, FieldSpec
from sniffer.domain.passport import Passport, has_value
from sniffer.domain.questions import Option, Question

MAX_QUESTIONS = 4
MIN_GAIN = 0.25
MAX_UNKNOWN_SHARE = 0.6
MAX_BUTTONS = 4
# callback_data — 64 байта на всё про кнопку; значение длиннее этого в неё не влезет.
MAX_VALUE_BYTES = 24
# Категория — не сужающий вопрос, а выбор предмета: в счёт потолка не идёт.
ROUTING_FIELDS = frozenset({"category", "city"})


@dataclass(frozen=True, slots=True)
class Search:
    """Искать и показывать. `reason` нужен журналу и тестам, клиенту он не виден."""

    reason: Literal["small", "show_all", "no_useful_question", "budget_spent"]


@dataclass(frozen=True, slots=True)
class Ask:
    question: Question


def expected_rest(facet: Facet) -> float:
    """Сколько карточек в среднем останется после ответа по этому полю."""
    known = facet.known
    if known == 0:
        return float("inf")
    return facet.unknown + sum(item.count**2 for item in facet.values) / known


def _label(value: str) -> str:
    """«air_blade» → «Air Blade», «sym» → «SYM»: короткие слова — марки-аббревиатуры."""
    words = value.replace("_", " ").split()
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in words)


def _vnd_label(edge: int) -> str:
    if edge >= 1_000_000:
        return f"до {edge / 1_000_000:g} млн"
    return f"до {edge // 1000} тыс"


def _category_options(facet: Facet) -> list[Option]:
    fits = [item for item in facet.values if len(item.value.encode()) <= MAX_VALUE_BYTES]
    return [Option(f"{_label(item.value)} {item.count}", item.value) for item in fits[:MAX_BUTTONS]]


def _price_options(facet: Facet) -> list[Option]:
    """Кнопки цены считают нарастающим итогом: «до 15 млн» включает и всё, что дешевле."""
    options: list[Option] = []
    running = 0
    for item in facet.values:
        running += item.count
        if item.value != OPEN_END:
            options.append(Option(f"{_vnd_label(int(item.value))} {running}", f"{item.value} VND"))
    return options[:MAX_BUTTONS]


_OPTIONS: Mapping[str, Callable[[Facet], list[Option]]] = {"budget.max": _price_options}


def _varies(facet: Facet) -> bool:
    """Ответ режет выдачу, только если значений хотя бы два или есть что отсечь."""
    return len(facet.values) >= 2


class ClarificationPlanner:
    def __init__(
        self,
        specs: Sequence[FieldSpec] = SPECS,
        *,
        target: int = TARGET_SIZE,
        max_questions: int = MAX_QUESTIONS,
        min_gain: float = MIN_GAIN,
    ) -> None:
        self._specs = [spec for spec in specs if spec.facet is not None]
        self._target = target
        self._max_questions = max_questions
        self._min_gain = min_gain

    def decide(self, passport: Passport, report: FacetReport, state: DialogueState) -> Search | Ask:
        if state.show_all:
            return Search("show_all")
        if report.total <= self._target:
            return Search("small")
        spent = [name for name in state.asked if name not in ROUTING_FIELDS]
        if len(spent) >= self._max_questions:
            return Search("budget_spent")
        best: tuple[float, FieldSpec, Facet] | None = None
        for spec in self._specs:
            facet = report.facets.get(spec.facet or "")
            if facet is None or not self._worth_asking(passport, state, spec, facet, report):
                continue
            gain = 1 - expected_rest(facet) / report.total
            if gain >= self._min_gain and (best is None or gain > best[0]):
                best = (gain, spec, facet)
        if best is None:
            return Search("no_useful_question")
        return Ask(self._question(best[1], best[2], report.total))

    @staticmethod
    def _worth_asking(
        passport: Passport, state: DialogueState, spec: FieldSpec, facet: Facet, report: FacetReport
    ) -> bool:
        return (
            not has_value(passport, spec.field)
            and spec.field not in state.asked
            and facet.unknown <= MAX_UNKNOWN_SHARE * report.total
            and _varies(facet)
            and bool(_OPTIONS.get(spec.field, _category_options)(facet))
        )

    @staticmethod
    def _question(spec: FieldSpec, facet: Facet, total: int) -> Question:
        options = _OPTIONS.get(spec.field, _category_options)(facet)
        note = f", у {facet.unknown} это не указано" if facet.unknown else ""
        return Question(
            field=spec.field,
            code=spec.code,
            text=f"Подходит {total}{note}. {spec.question.text}",
            options=tuple(options),
            skip_label="Любой",
            show_all=total,
        )
