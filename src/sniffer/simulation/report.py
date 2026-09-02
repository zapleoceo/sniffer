"""Отчёт глазами: таблица «сценарий → метрики → вердикт».

Владельцу нужно не «тесты зелёные», а число: сколько вопросов бот задаёт до
первой выдачи и сколько показанного противоречит запросу. Таблица печатается
целиком и всегда, включая удачные строки, — иначе не видно, что улучшилось.

Дефекты разделены на три блока, и это не оформление. **Дефект диалога** —
регресс, из-за него отчёт возвращает ненулевой код. **Дефект выдачи** —
сегодняшнее системное свойство поиска (ранжируем, но не отсекаем), красить им
весь прогон бессмысленно. **Известный пробел** — то, что ещё не сделано, с
причиной: без этого блока красная строка и незаконченная работа выглядят
одинаково.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sniffer.simulation.harness import Metrics
from sniffer.simulation.verdict import dialogue_faults, relevance_faults, wish_faults

Judge = Callable[[Metrics], tuple[str, ...]]

_TITLE_WIDTH = 44
_HEADERS = ("сценарий", "?дo", "?всего", "ходов", "карт", "мимо", "стар", "вердикт")
_WIDTHS = (_TITLE_WIDTH, 4, 6, 5, 4, 4, 4, 8)


def render_report(runs: Sequence[Metrics]) -> str:
    """Весь отчёт одной строкой текста — печатать его дело вызывающего."""
    lines = [_row(_HEADERS), _rule()]
    lines += [_row(_cells(metrics)) for metrics in runs]
    lines.append("")
    lines += _summary(runs)
    lines += _block("Дефекты диалога — это регресс", runs, dialogue_faults)
    lines += _block("Дефекты выдачи — показано мимо запроса", runs, relevance_faults)
    lines += _wishes(runs)
    return "\n".join(lines)


def render_replies(metrics: Metrics) -> str:
    """Переписка одного сценария целиком — когда цифры уже не объясняют."""
    lines = [f"── {metrics.scenario.key}: {metrics.scenario.title}"]
    for number, text in enumerate(metrics.replies, start=1):
        body = "\n     ".join(text.splitlines())
        lines.append(f"  {number}. {body}")
    return "\n".join(lines)


def _cells(metrics: Metrics) -> tuple[str, ...]:
    turns = metrics.client_turns_to_results
    return (
        metrics.scenario.title,
        str(metrics.questions_before_results),
        str(metrics.questions_total),
        "—" if turns is None else str(turns),
        str(metrics.cards_shown),
        str(len(metrics.off_target)),
        str(metrics.stale_shown),
        _verdict(metrics),
    )


def _verdict(metrics: Metrics) -> str:
    if dialogue_faults(metrics):
        return "ДИАЛОГ"
    if relevance_faults(metrics):
        return "выдача"
    return "ok"


def _summary(runs: Sequence[Metrics]) -> list[str]:
    broken = [metrics for metrics in runs if dialogue_faults(metrics)]
    dirty = [metrics for metrics in runs if relevance_faults(metrics)]
    shown = sum(metrics.cards_shown for metrics in runs)
    missed = sum(len(metrics.off_target) for metrics in runs)
    asked = sum(metrics.questions_before_results for metrics in runs)
    return [
        f"сценариев: {len(runs)} · дефектов диалога: {len(broken)} · "
        f"сценариев с мусором в выдаче: {len(dirty)}",
        f"вопросов до выдачи суммарно: {asked} · карточек показано: {shown} · "
        f"из них мимо запроса: {missed}" + (f" ({missed * 100 // shown}%)" if shown else ""),
        "",
    ]


def _block(title: str, runs: Sequence[Metrics], judge: Judge) -> list[str]:
    found = _judged(runs, judge)
    if not found:
        return [f"{title}: нет", ""]
    lines = [f"{title}:"]
    for metrics, reasons in found:
        lines.append(f"  {metrics.scenario.key}")
        lines += [f"    · {reason}" for reason in reasons]
    lines.append("")
    return lines


def _judged(runs: Sequence[Metrics], judge: Judge) -> list[tuple[Metrics, tuple[str, ...]]]:
    return [(metrics, judge(metrics)) for metrics in runs if judge(metrics)]


def _wishes(runs: Sequence[Metrics]) -> list[str]:
    found = _judged(runs, wish_faults)
    if not found:
        return ["Известные пробелы: закрыты все", ""]
    lines = ["Известные пробелы — работа не сделана, а не сломана:"]
    for metrics, reasons in found:
        wish = metrics.scenario.wish
        lines.append(f"  {metrics.scenario.key}")
        lines += [f"    · {reason}" for reason in reasons]
        if wish is not None:
            lines.append(f"    почему: {wish.why}")
    lines.append("")
    return lines


def _row(cells: Sequence[str]) -> str:
    return "  ".join(_fit(cell, width) for cell, width in zip(cells, _WIDTHS, strict=True))


def _rule() -> str:
    return "  ".join("─" * width for width in _WIDTHS)


def _fit(cell: str, width: int) -> str:
    text = cell if len(cell) <= width else f"{cell[: width - 1]}…"
    return text.ljust(width)
