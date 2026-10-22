"""Команда `python -m sniffer.worker stale-cleanup`: уборка карточек старше срока.

**Выключена по умолчанию и без расписания.** Без флагов — сухой прогон: те же
выборки и счётчики, но ни одной записи. Удаление требует двух явных действий
сразу: `--execute` и `--confirm DELETE-STALE-<дата границы>` (дата — UTC-день
границы, `now - LISTING_MAX_AGE_DAYS`, посчитанной один раз в начале). Подтверждение
привязано к дате, чтобы строка из вчерашней заметки не сработала сегодня. Запускать
боевой прогон — только по решению владельца и после проверки свежей резервной
копии (`docs/deploy.md`, раздел про уборку).

Правила защиты и каскады — в `db/repositories/listing_cleanup.py`. Сырьё
(`raw_messages`) не затрагивается: срок хранения у него свой.

Коды выхода, как у `enrich`: 0 — проход закончен; 2 — неверные аргументы или
подтверждение; 130 — прервали; 1 — всё остальное. Тексты сбоев не печатаются,
только имена классов: в тексте ошибки базы лежат параметры SQL.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.config import get_settings
from sniffer.db.engine import dispose_engine, session_scope
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.listing_state import listing_cutoff
from sniffer.runtime.exits import class_chain, is_interrupt, reraise_if_not_ours
from sniffer.runtime.logs import setup_logging

log = structlog.get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_BATCH = 500
MAX_BATCH = 5000
DEFAULT_SLEEP_S = 0.5
MAX_SLEEP_S = 60.0

USAGE = (
    "Использование: python -m sniffer.worker stale-cleanup [--dry-run]\n"
    "    [--batch-size N] [--sleep-s S]\n"
    "  python -m sniffer.worker stale-cleanup --execute --confirm DELETE-STALE-YYYY-MM-DD\n"
    "  по умолчанию — сухой прогон (ничего не удаляет);\n"
    "  --execute      удалять (нужен --confirm с датой границы, её печатает сухой прогон);\n"
    f"  --batch-size N размер пачки, 1..{MAX_BATCH} (по умолчанию {DEFAULT_BATCH});\n"
    f"  --sleep-s S    пауза между пачками, 0..{MAX_SLEEP_S:g} с (по умолчанию {DEFAULT_SLEEP_S})."
)
HELP_FLAGS = ("-h", "--help")

Now = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
Out = Callable[[str], None]
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class UsageError(ValueError):
    """Аргументы или подтверждение неверны; текст — для человека."""


@dataclass(frozen=True, slots=True)
class Options:
    execute: bool = False
    confirm: str | None = None
    batch_size: int = DEFAULT_BATCH
    sleep_s: float = DEFAULT_SLEEP_S


def confirmation_for(cutoff: datetime) -> str:
    return f"DELETE-STALE-{cutoff.astimezone(UTC):%Y-%m-%d}"


def parse_options(argv: Sequence[str]) -> Options:
    """Строгий разбор: опечатка не превращается в боевой прогон."""
    flags: set[str] = set()
    values: dict[str, str] = {}
    queue = list(argv)
    while queue:
        arg = queue.pop(0)
        name, equals, glued = arg.partition("=")
        if name in ("--dry-run", "--execute"):
            if equals or name in flags:
                raise UsageError(f"{name}: без значения и не более одного раза")
            flags.add(name)
        elif name in ("--confirm", "--batch-size", "--sleep-s"):
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
    if {"--dry-run", "--execute"} <= flags:
        raise UsageError("--dry-run и --execute несовместимы")
    if "--confirm" in values and "--execute" not in flags:
        raise UsageError("--confirm имеет смысл только вместе с --execute")
    batch = values.get("--batch-size", str(DEFAULT_BATCH))
    sleep = values.get("--sleep-s", str(DEFAULT_SLEEP_S))
    if not re.fullmatch(r"[0-9]{1,6}", batch) or not 1 <= int(batch) <= MAX_BATCH:
        raise UsageError(f"--batch-size: нужно целое от 1 до {MAX_BATCH}, получено {batch!r}")
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3})?", sleep) or float(sleep) > MAX_SLEEP_S:
        raise UsageError(f"--sleep-s: нужно число от 0 до {MAX_SLEEP_S:g}, получено {sleep!r}")
    return Options(
        execute="--execute" in flags,
        confirm=values.get("--confirm"),
        batch_size=int(batch),
        sleep_s=float(sleep),
    )


@dataclass(slots=True)
class CleanupReport:
    """Итоги прохода. Только числа и даты: ни текстов, ни заголовков."""

    execute: bool = False
    cutoff: datetime | None = None
    batches: int = 0
    selected: int = 0
    deleted: int = 0
    media: int = 0
    notifications: int = 0
    stale_total: int = 0
    skipped_catalog: int = 0
    skipped_offer_views: int = 0

    def render(self) -> str:
        mode = "УДАЛЕНИЕ" if self.execute else "СУХОЙ ПРОГОН (ничего не удалено)"
        cut = self.cutoff.isoformat() if self.cutoff else "-"
        verb = "ушло каскадом" if self.execute else "каскадом уйдёт"
        return (
            f"stale-cleanup: {mode}\n"
            f"  граница (UTC): {cut}\n"
            f"  старше границы всего: {self.stale_total}\n"
            f"  пропущено, проекция каталога (catalog_observation_id): {self.skipped_catalog}\n"
            f"  пропущено, есть показы (offer_views): {self.skipped_offer_views}\n"
            f"  пачек: {self.batches}; выбрано: {self.selected}; удалено: {self.deleted}\n"
            f"  {verb}: listing_media {self.media}, notifications {self.notifications}"
        )


async def run_cleanup(
    options: Options,
    report: CleanupReport,
    *,
    now: Now = lambda: datetime.now(UTC),
    sleep: Sleep = asyncio.sleep,
    session_factory: SessionFactory = session_scope,
) -> CleanupReport:
    """Граница считается ОДИН раз; подтверждение сверяется до первого запроса."""
    cutoff = listing_cutoff(now())
    report.execute, report.cutoff = options.execute, cutoff
    expected = confirmation_for(cutoff)
    if options.execute and options.confirm != expected:
        raise UsageError(f"--confirm должен быть ровно {expected!r} (дата границы)")
    log.info("stale_cleanup.start", cutoff=cutoff.isoformat(), execute=options.execute)
    async with session_factory() as session:
        overview = await ListingRepository(session).overview_stale(cutoff)
    report.stale_total = overview.stale_total
    report.skipped_catalog = overview.protected_catalog
    report.skipped_offer_views = overview.protected_offer_views
    last_id = 0
    while True:
        async with session_factory() as session:
            batch = await ListingRepository(session).delete_stale(
                cutoff, options.batch_size, dry_run=not options.execute, after_id=last_id
            )
            await session.commit()
        if batch.selected == 0:
            break
        last_id = batch.last_id
        report.batches += 1
        report.selected += batch.selected
        report.deleted += batch.deleted
        report.media += batch.media
        report.notifications += batch.notifications
        log.info(
            "stale_cleanup.batch",
            selected=batch.selected,
            deleted=batch.deleted,
            media=batch.media,
            notifications=batch.notifications,
            last_id=last_id,
        )
        await sleep(options.sleep_s)
    log.info(
        "stale_cleanup.done",
        batches=report.batches,
        selected=report.selected,
        deleted=report.deleted,
    )
    return report


Runner = Callable[[Options, CleanupReport], Coroutine[Any, Any, CleanupReport]]


async def _run_real(options: Options, report: CleanupReport) -> CleanupReport:
    setup_logging(get_settings().log_level)
    try:
        return await run_cleanup(options, report)
    finally:
        await dispose_engine()


def _stdout(text: str) -> None:
    print(text, flush=True)


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _say(write: Out, text: str) -> None:
    try:
        write(text)
    except BaseException as caught:
        reraise_if_not_ours(caught)


def run_stale_cleanup(
    argv: Sequence[str], *, runner: Runner = _run_real, out: Out = _stdout, err: Out = _stderr
) -> int:
    report = CleanupReport()
    if any(arg in HELP_FLAGS for arg in argv):
        _say(out, USAGE)
        return EXIT_OK
    try:
        options = parse_options(argv)
        asyncio.run(runner(options, report))
    except UsageError as problem:
        _say(err, f"{problem}\n{USAGE}")
        return EXIT_USAGE
    except BaseException as caught:
        reraise_if_not_ours(caught)
        interrupted = is_interrupt(caught)
        outcome = "Прервано." if interrupted else f"Остановлено: {class_chain(caught)}."
        note = (
            "Повтор безопасен: уборка идемпотентна." if report.execute else "Ничего не удалялось."
        )
        _say(err, f"{outcome} {note}")
        _say(out, report.render())
        return EXIT_INTERRUPTED if interrupted else EXIT_FAILED
    _say(out, report.render())
    return EXIT_OK
