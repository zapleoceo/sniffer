"""Проход догона: накопленные карточки, пересчитанные из исходного текста.

Разовый проход, а не задача цикла воркера: запускается руками после деплоя
(`python -m sniffer.worker enrich`, runbook — `docs/deploy.md`, 7.2), потому что
перед ним нужна копия таблицы и чтение сухого прогона человеком. Правила
пересчёта — реестр выводов (`pipeline/enrich.py`), запись — репозиторий
(`db/repositories/listing_enrichment.py`); здесь только обход и учёт.

* **Курсор по `listings.id`**, пачки по 200: страница читается своей короткой
  транзакцией, патчи считаются в памяти, запись — другой транзакцией. Строки,
  появившиеся во время прохода, лежат правее курсора и подхватываются в конце.
* **Одна кривая не рушит пачку.** Вывод, не справившийся с карточкой, пропускает
  её и называет себя; запись каждой карточки изолирована (SAVEPOINT в
  репозитории, `write_each` здесь), и отказ базы на одной строке стоит одной
  строки.
* **Сухой прогон не пишет вообще.** Запись вообще не вызывается, а транзакция
  чтения не коммитится: даже будь в коде ошибка, база осталась бы прежней.
* **Идемпотентен.** В патче только разница, поэтому повторный проход пуст и в
  базу не ходит.
* **Рядом с живым воркером.** Запись — сравнение с обменом: строка меняется
  лишь если осталась такой, какой её прочитали; изменившаяся пропускается и
  считается в отчёте (подробности — репозиторий).
* **Не трогает** `is_active`, `screened_at`, `posted_at`, `deal_type`,
  `category`: колонки патча — закрытый список (`domain/listing_patch.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.listing_enrichment import ListingEnrichmentRepository
from sniffer.domain.listing_patch import ListingPatch, ListingWithText, WriteResult, WriteStatus
from sniffer.domain.records import Listing
from sniffer.pipeline import enrich_price as price
from sniffer.pipeline.enrich import derive
from sniffer.runtime.exits import class_chain
from sniffer.worker.enrich_origin import changed_by_verdict
from sniffer.worker.enrich_report import AFTER_VERDICT, END, LIMIT, EnrichReport

log = structlog.get_logger(__name__)

SOURCE = "telegram_archive"
PAGE = 200
# Построчные предупреждения ограничены: баг вывода на каждой карточке дал бы
# 18 тысяч одинаковых строк. Дальше считаем, а не пишем — итог в отчёте.
MAX_ROW_WARNINGS = 20

# Исходы, что меняют колонки цены: только им имеет смысл спрашивать, не сменил ли
# вердикт модели сторону или категорию (остальные ничего не пишут в колонку).
VERDICT_TRACKED = frozenset({price.FILLED, price.REPLACED, price.ERASED})

Item = tuple[ListingWithText, ListingPatch]
Page = Callable[[int, int], Awaitable[list[ListingWithText]]]
Write = Callable[[list[Item]], Awaitable[list[WriteResult]]]
Derive = Callable[[Listing, str], ListingPatch]
ByVerdict = Callable[[Listing, str], bool]


class CursorStalled(RuntimeError):
    """Страница не сдвинула курсор: следующий круг прочёл бы то же самое.

    Без этой остановки сбой источника страниц (id не по возрастанию, повтор
    страницы) превращался в бесконечный проход, который перечитывает и
    переписывает одни и те же карточки; зависание хуже ошибки, ошибку видно.
    """


class Applier(Protocol):
    async def apply(self, row: Listing, patch: ListingPatch) -> bool: ...


async def write_each(applier: Applier, items: Sequence[Item]) -> list[WriteResult]:
    """Записать патчи по одному; отказ одной карточки не уносит соседей.

    Широкий `except Exception` намеренно: перечислять причины, по которым база
    не приняла чужой текст, значит однажды встать на неназванной (так упала
    воронка 01.09.2026). Исход один для любой — карточка пропущена и названа
    по классу сбоя, без текста: в нём параметры SQL. Прерывание (`BaseException`)
    сюда не попадает и останавливает проход целиком, как и должно.
    """
    results: list[WriteResult] = []
    for item, patch in items:
        try:
            done = await applier.apply(item.listing, patch)
        except Exception as err:
            results.append(WriteResult(WriteStatus.FAILED, class_chain(err)))
        else:
            results.append(WriteResult(WriteStatus.APPLIED if done else WriteStatus.STALE))
    return results


async def _page(after_id: int, limit: int) -> list[ListingWithText]:
    async with session_scope() as session:
        return await ListingEnrichmentRepository(session).page(
            SOURCE, after_id=after_id, limit=limit
        )


async def _write(items: list[Item]) -> list[WriteResult]:
    async with session_scope() as session:
        results = await write_each(ListingEnrichmentRepository(session), items)
        await session.commit()
    return results


def _last_id(rows: list[ListingWithText]) -> int:
    last = rows[-1].listing.id
    assert last is not None
    return last


class EnrichPass:
    def __init__(
        self,
        *,
        page: Page = _page,
        write: Write = _write,
        derive_row: Derive = derive,
        by_verdict: ByVerdict = changed_by_verdict,
        size: int = PAGE,
    ) -> None:
        self._page, self._write, self._derive, self._size = page, write, derive_row, size
        self._by_verdict = by_verdict
        self._warned = 0

    async def run(
        self,
        report: EnrichReport,
        *,
        dry_run: bool = False,
        limit: int | None = None,
        since_id: int = 0,
    ) -> None:
        """Обойти карточки правее `since_id`, не больше `limit`; итог — в `report`.

        Итоговое событие уходит в лог при любом исходе, в том числе при сбое и
        прерывании: оператору нужно знать, докуда дошли (`last_id`), чтобы
        продолжить с `--since-id`.
        """
        report.dry_run, report.last_id = dry_run, since_id
        cursor, remaining = since_id, limit
        try:
            while remaining is None or remaining > 0:
                take = self._size if remaining is None else min(self._size, remaining)
                rows = await self._page(cursor, take)
                if not rows:
                    report.stop_reason = END
                    return
                last = _last_id(rows)
                if last <= cursor:
                    raise CursorStalled(f"страница после id {cursor} закончилась на id {last}")
                await self._batch(rows, report, dry_run=dry_run)
                cursor = report.last_id = last
                if remaining is not None:
                    remaining -= len(rows)
                totals = report.totals()
                log.info(
                    "enrich.batch",
                    last_id=cursor,
                    seen=totals["seen"],
                    written=totals["would_write" if dry_run else "written"],
                    skipped=report.skipped(),
                )
            report.stop_reason = LIMIT
        finally:
            log.info("enrich.report", **report.fields())

    def _warn(self, **fields: object) -> None:
        self._warned += 1
        if self._warned <= MAX_ROW_WARNINGS:
            log.warning("enrich.row_failed", **fields)

    def _plan(self, item: ListingWithText, report: EnrichReport) -> ListingPatch | None:
        """Патч карточки или `None`, если её пришлось пропустить (причина в отчёте)."""
        if item.text is None:
            report.skip(item.listing, "no_text")
            return None
        try:
            return self._derive(item.listing, item.text)
        except Exception as err:
            # Любая причина (разбор чужого текста, спор выводов за поле, баг
            # вывода) для прохода одна: карточку пропустить и назвать вывод.
            name = getattr(err, "derivation", "")
            report.skip(item.listing, f"derive_error.{name}" if name else "derive_error")
            self._warn(listing_id=item.listing.id, step="derive", error=class_chain(err))
            return None

    async def _batch(
        self, rows: list[ListingWithText], report: EnrichReport, *, dry_run: bool
    ) -> None:
        planned: list[Item] = []
        for item in rows:
            report.seen(item.listing)
            patch = self._plan(item, report)
            if patch is None:
                continue
            if patch.is_empty:
                # Писать нечего, и исход уже окончательный: «верно», «расхождение».
                report.outcomes(item.listing, patch.outcomes)
            else:
                planned.append((item, patch))
        if planned:
            await self._flush(planned, report, dry_run=dry_run)

    def _count(self, item: ListingWithText, patch: ListingPatch, report: EnrichReport) -> None:
        """Исходы карточки в отчёт, с пометкой «после смены стороны или категории».

        Цена читается под итоговой стороной, а вердикт модели приходит после
        создания карточки: сколько записей объясняется именно им, владелец должен
        видеть до боевого прогона. Опрос (`changed_by_verdict`) — разбор запроса
        на карточку, поэтому только для исходов, что меняют колонку.
        """
        report.outcomes(item.listing, patch.outcomes)
        if price.ERASED in patch.outcomes:
            report.erased_price(item.listing)
        changed = [o for o in patch.outcomes if o in VERDICT_TRACKED]
        if not changed or item.text is None:
            return
        try:
            flipped = self._by_verdict(item.listing, item.text)
        except Exception as err:
            # Пометка — справка к отчёту, а не условие записи: сбой разбора
            # запроса не стоит карточки, но и молчать о нём нельзя.
            self._warn(listing_id=item.listing.id, step="verdict", error=class_chain(err))
            report.outcomes(item.listing, [AFTER_VERDICT + "unknown"])
            return
        if flipped:
            names = [AFTER_VERDICT + outcome.removeprefix(f"{price.NAME}.") for outcome in changed]
            report.outcomes(item.listing, names)

    async def _flush(self, planned: list[Item], report: EnrichReport, *, dry_run: bool) -> None:
        if dry_run:
            for item, patch in planned:
                report.wrote(item.listing)
                self._count(item, patch, report)
            return
        try:
            results = await self._write(planned)
        except BaseException:
            # Не проглатываем, а доучитываем: пачка не записалась целиком, и
            # отчёт, который напечатают при сбое, должен сходиться по счёту.
            for item, _ in planned:
                report.skip(item.listing, "batch_failed")
            raise
        for (item, patch), result in zip(planned, results, strict=True):
            if result.status is WriteStatus.APPLIED:
                report.wrote(item.listing)
                self._count(item, patch, report)
            elif result.status is WriteStatus.STALE:
                report.skip(item.listing, "changed_since_read")
            else:
                report.skip(item.listing, "write_error")
                self._warn(listing_id=item.listing.id, step="write", error=result.error)
