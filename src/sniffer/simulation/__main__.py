"""`python -m sniffer.simulation` — отчёт о качестве диалога цифрами.

Сервисом не является: отработала и вышла. Сеть, модель и Postgres не нужны, а
значит запускать можно и на ноутбуке, и в боевом контейнере — второе и есть
смысл затеи: зелёный CI не доказывает, что на сервере то же самое.

Код возврата: `1`, если есть дефекты ДИАЛОГА (это регресс), иначе `0`. Мусор в
выдаче кода не меняет — сегодня он системный, и красный на нём стоял бы всегда,
то есть не значил бы ничего.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sniffer.simulation.harness import run_all
from sniffer.simulation.report import render_replies, render_report
from sniffer.simulation.scenarios import SCENARIOS
from sniffer.simulation.verdict import dialogue_faults


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniffer.simulation", description=__doc__)
    parser.add_argument("--scenario", default="", help="прогнать один сценарий по ключу")
    parser.add_argument("--replies", action="store_true", help="показать переписку целиком")
    args = parser.parse_args(argv)

    chosen = [item for item in SCENARIOS if not args.scenario or item.key == args.scenario]
    if not chosen:
        keys = ", ".join(item.key for item in SCENARIOS)
        print(f"нет сценария {args.scenario!r}. Есть: {keys}")
        return 2

    runs = asyncio.run(run_all(chosen))
    print(render_report(runs))
    if args.replies:
        for metrics in runs:
            print(render_replies(metrics))
            print()
    return 1 if any(dialogue_faults(metrics) for metrics in runs) else 0


if __name__ == "__main__":
    # Кириллица в консоли Windows иначе падает UnicodeEncodeError на cp1252 —
    # отчёт о качестве не должен зависеть от кодовой страницы терминала.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
