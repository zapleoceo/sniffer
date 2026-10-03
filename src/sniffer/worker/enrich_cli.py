"""Команда `python -m sniffer.worker enrich [--dry-run] [--limit N] [--since-id ID]`.

Разовая ручная операция: пересчитать накопленные карточки из исходного текста
(`worker/enrich.py`). Живёт подкомандой образа воркера, а не отдельным
скриптом: ей нужны ровно тот же код разбора и та же версия зависимостей, что у
воронки, иначе пересчёт и новые карточки считали бы цену по-разному.

Исходов у команды четыре, и это закрытый набор, описанный в `docs/deploy.md`
(7.2): 0 — проход закончился и отчёт напечатан; 2 — неверные аргументы; 130 —
прервали (Ctrl+C); 1 — всё остальное. Код, которого нет в этой таблице,
считается дефектом.

**Полнота набора держится построением, а не списком.** Каждый шаг, который
делает работу (разбор аргументов, сам проход), стоит внутри блока, чей
последний `except` — `BaseException`, корень иерархии, а не `Exception` и не
список ожидаемых классов (CLAUDE.md, «Как закрывают набор кодов возврата»).
Перебрасывается только то, что не поломка, а требование остановиться
(`runtime/exits.py`). Вне охраны остаётся одно: форматирование наших же строк
для вывода — баг в нём прятать не за что.

**Тексты сбоев не печатаются, только имена классов.** В тексте исключения базы
лежат параметры SQL, а параметры записи — заголовки и атрибуты объявлений с
телефонами и @username; вывод команды остаётся в `docker logs` и в истории
терминала.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass

from sniffer.config import get_settings
from sniffer.db.engine import dispose_engine
from sniffer.runtime.exits import class_chain, is_interrupt, reraise_if_not_ours
from sniffer.runtime.logs import setup_logging
from sniffer.worker.enrich import EnrichPass
from sniffer.worker.enrich_report import EnrichReport

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# 128 + SIGINT — то, что оболочка ожидает увидеть после Ctrl+C.
EXIT_INTERRUPTED = 130

USAGE = (
    "Использование: python -m sniffer.worker enrich [--dry-run] [--limit N] [--since-id ID]\n"
    "  --dry-run      ничего не писать в базу, только отчёт;\n"
    "  --limit N      просмотреть не больше N карточек (N ≥ 1);\n"
    "  --since-id ID  начать с карточек правее этого id — так продолжают прерванный проход."
)

HELP_FLAGS = ("-h", "--help")
# Больше не бывает: `listings.id` — BIGSERIAL. Дальше база ответила бы ошибкой
# драйвера, а не словами.
MAX_ID = 2**63 - 1

Runner = Callable[["Options", EnrichReport], Coroutine[None, None, None]]
Out = Callable[[str], None]


class UsageError(ValueError):
    """Аргументы команды неверны; текст — для человека."""


@dataclass(frozen=True, slots=True)
class Options:
    dry_run: bool = False
    limit: int | None = None
    since_id: int = 0


def _integer(name: str, raw: str, *, minimum: int) -> int:
    if not re.fullmatch(r"[0-9]+", raw):
        raise UsageError(f"{name}: нужно целое число, получено {raw!r}")
    value = int(raw)
    if value < minimum or value > MAX_ID:
        raise UsageError(f"{name}: нужно число от {minimum}, получено {raw!r}")
    return value


def parse_options(argv: Sequence[str]) -> Options:
    """Строгий разбор: лишнее, повторное и недописанное — ошибка, а не умолчание.

    Опечатка в `--dry-run` не должна превращаться в боевой прогон: аргумент,
    который не распознан, останавливает команду, а не игнорируется. Сокращений
    (`--dry`) нет по той же причине.
    """
    dry_run = False
    values: dict[str, str] = {}
    queue = list(argv)
    while queue:
        arg = queue.pop(0)
        name, equals, glued = arg.partition("=")
        if name == "--dry-run":
            if equals:
                raise UsageError("--dry-run не принимает значения")
            if dry_run:
                raise UsageError("--dry-run указан дважды")
            dry_run = True
        elif name in ("--limit", "--since-id"):
            if name in values:
                raise UsageError(f"{name} указан дважды")
            if equals:
                values[name] = glued
            elif queue:
                values[name] = queue.pop(0)
            else:
                raise UsageError(f"{name} требует значения")
        else:
            raise UsageError(f"неизвестный аргумент: {arg!r}")
    limit = values.get("--limit")
    since = values.get("--since-id")
    return Options(
        dry_run=dry_run,
        limit=_integer("--limit", limit, minimum=1) if limit is not None else None,
        since_id=_integer("--since-id", since, minimum=0) if since is not None else 0,
    )


async def run_pass(options: Options, report: EnrichReport) -> None:
    """Настоящий исполнитель: логи, проход, закрытие пула соединений."""
    setup_logging(get_settings().log_level)
    try:
        await EnrichPass().run(
            report, dry_run=options.dry_run, limit=options.limit, since_id=options.since_id
        )
    finally:
        await dispose_engine()


def _stdout(text: str) -> None:
    print(text, flush=True)


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _say(write: Out, text: str) -> None:
    """Сказать, не рискуя исходом: вывод — не безотказный канал.

    `… | head -1` закрывает stdout, том докеровского лога переполняется
    (`OSError`), оболочка присылает Ctrl+C — а исход команды к этому моменту уже
    определён. Охрана до корня иерархии: ни причина отказа вывода, ни прерывание
    на самом последнем действии не вправе превратить проход в трейсбек.
    """
    try:
        write(text)
    except BaseException as caught:
        reraise_if_not_ours(caught)


def _failed(caught: BaseException, report: EnrichReport, *, out: Out, err: Out) -> int:
    """Ответ шага на ЛЮБУЮ поломку: слова, как далеко дошли, и документированный код."""
    reraise_if_not_ours(caught)
    consequence = (
        "Сухой прогон ничего не записывал."
        if report.dry_run
        else "Записанное остаётся записанным: проход идемпотентен, его можно повторить."
    )
    if is_interrupt(caught):
        _say(err, f"Прервано. {consequence}")
        code = EXIT_INTERRUPTED
    else:
        _say(err, f"Проход остановлен: {class_chain(caught)}. {consequence}")
        code = EXIT_FAILED
    _say(out, report.render())
    return code


def run_enrich(
    argv: Sequence[str],
    *,
    runner: Runner = run_pass,
    out: Out = _stdout,
    err: Out = _stderr,
) -> int:
    """Команда целиком: разбор, проход, отчёт. Возвращает код выхода.

    Каждый шаг, который делает РАБОТУ, стоит внутри блока, чей последний
    `except` — `BaseException`: так список ожидаемых классов не может оказаться
    неполным (пять кругов на `collector auth` показали, что оказывается, и что
    `Exception` — не корень). Отчёт при сбое всё равно печатается: оператору
    нужно знать, докуда дошли, чтобы продолжить с `--since-id`.
    """
    report = EnrichReport()
    if any(arg in HELP_FLAGS for arg in argv):
        _say(out, USAGE)
        return EXIT_OK
    try:
        options = parse_options(argv)
    except UsageError as problem:
        _say(err, f"{problem}\n{USAGE}")
        return EXIT_USAGE
    except BaseException as caught:
        return _failed(caught, report, out=out, err=err)
    try:
        report.dry_run = options.dry_run
        asyncio.run(runner(options, report))
    except BaseException as caught:
        return _failed(caught, report, out=out, err=err)
    _say(out, report.render())
    return EXIT_OK
