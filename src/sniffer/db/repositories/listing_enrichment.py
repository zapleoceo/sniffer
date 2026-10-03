"""Догон карточек: чтение «карточка + исходный текст» и запись патча.

Проход догона (`worker/enrich.py`) работает РЯДОМ с живым воркером: пока он
считает патч, воронка может освежить карточку кросспостом (другой текст,
другая цена), проверка модели — сменить категорию и сторону, пересчёт категорий
— переписать атрибуты. Запись, посчитанная по устаревшей строке, перетёрла бы
чужое решение. Поэтому запись — это сравнение с обменом: **строка обновляется
только если она осталась такой, какой её прочитали** (`unchanged_since`).

Почему не версия и не блокировка. Колонки версии у `listings` нет, и заводить её
значит менять схему и каждого писателя ради одного прохода. Блокировка
(`SELECT … FOR UPDATE`) держала бы строку на время работы Python и заставляла бы
воркер ждать нас. Сравнение значений не держит ничего: условие `WHERE` читает
строку в момент записи, а Postgres на `READ COMMITTED` перепроверяет его по
последней зафиксированной версии, если строку успели изменить. Цена ошибки
(строка изменилась) — не запись и счёт в отчёте, а повторный проход всё равно
подхватит: он идемпотентен.

Атрибуты пишутся на стороне базы как `(attributes - удаляемые) || патч`: ключи,
которых проход не знает, не теряются, а ключи, которые вывод больше не вправе
держать (суточная ставка от прежней стороны сделки), убираются тем же UPDATE.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import ColumnElement, Select, Table, Text, Update, bindparam, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from sniffer.db import models
from sniffer.db.mappers import to_listing
from sniffer.db.repositories.base import Repository
from sniffer.domain.listing_patch import ListingPatch, ListingWithText
from sniffer.domain.records import Listing

# Что строка обязана сохранить с момента чтения, чтобы запись по ней была верна:
# и то, что патч пишет (иначе решение «заполнить» принято по пустому, которое
# уже не пусто), и то, что вывод читает (иначе цена посчитана по чужому тексту
# и чужим границам категории). `is_active` и `screened_at` сюда не входят: они
# ничего не меняют в верности патча, а гасить карточку можно в любой момент.
GUARD_COLUMNS = (
    "raw_message_id",  # репост освежил карточку: текст, из которого считали, уже другой
    "category",  # вход вывода: границы цены и справочники
    "deal_type",  # вход вывода: срок цены
    "city",  # вход вывода: справочник районов зависит от города
    "price_amount",
    "price_currency",
    "price_period",
    "district",
    "title",
    "lang",
    "attributes",
)


def _table() -> Table:
    return cast(Table, models.Listing.__table__)


def unchanged_since(table: Table, row: Listing) -> list[ColumnElement[bool]]:
    """Условие «строка осталась той, что прочитали».

    `IS NOT DISTINCT FROM`, а не `=`: у половины защищаемых колонок бывает NULL,
    а `NULL = NULL` не истина — карточка с пустым районом не обновилась бы
    никогда, и проход молча ничего бы не делал.
    """
    conditions = [table.c.id == row.id]
    conditions += [table.c[name].is_not_distinct_from(getattr(row, name)) for name in GUARD_COLUMNS]
    return conditions


def page_statement(source: str, *, after_id: int, limit: int) -> Select[Any]:
    """Запрос страницы: активные карточки источника правее курсора, по возрастанию id.

    `outerjoin`, а не `join`: карточка без сырья (его не должно быть, но каскад и
    ручные правки случаются) должна попасть в отчёт строкой «нет текста», а не
    исчезнуть из прохода молча.
    """
    return (
        select(models.Listing, models.RawMessage.text)
        .outerjoin(models.RawMessage, models.Listing.raw_message_id == models.RawMessage.id)
        .where(
            models.Listing.source == source,
            models.Listing.is_active.is_(True),
            models.Listing.id > after_id,
        )
        .order_by(models.Listing.id)
        .limit(limit)
        # Сессия может помнить прежнюю версию строки (тест, долгий процесс):
        # проход обязан видеть то, что в базе сейчас, а не то, что сессия
        # когда-то загрузила, иначе повторный проход не был бы идемпотентным.
        .execution_options(populate_existing=True)
    )


def patch_statement(row: Listing, patch: ListingPatch) -> Update:
    """UPDATE одной карточки: пишет разницу патча, если строка не изменилась."""
    if patch.is_empty:
        raise ValueError("пустой патч не пишется: вызывающий обязан отсеять его раньше")
    table = _table()
    values: dict[str, object] = dict(patch.columns)
    if patch.remove or patch.attributes:
        # `(attributes - удаляемые) || патч`: ключи вне патча целы, названные
        # убираются, ключи патча главнее — и всё одним UPDATE, без промежутка,
        # в котором карточка осталась бы без ставки или с чужой.
        attributes: ColumnElement[Any] = table.c.attributes
        if patch.remove:
            keys = bindparam("remove_keys", list(patch.remove), type_=ARRAY(Text))
            attributes = attributes.op("-", return_type=JSONB)(keys)
        if patch.attributes:
            attributes = attributes.concat(dict(patch.attributes))
        values["attributes"] = attributes
    return update(table).where(*unchanged_since(table, row)).values(**values)


class ListingEnrichmentRepository(Repository):
    async def page(self, source: str, *, after_id: int, limit: int) -> list[ListingWithText]:
        """Страница активных карточек источника по возрастанию id, с текстом сырья."""
        statement = page_statement(source, after_id=after_id, limit=limit)
        rows = (await self._session.execute(statement)).all()
        return [ListingWithText(to_listing(listing), text) for listing, text in rows]

    async def apply(self, row: Listing, patch: ListingPatch) -> bool:
        """Записать патч. `True` — записано; `False` — строка успела измениться.

        Запись идёт в своём SAVEPOINT: если база откажет на этой карточке
        (число не влезло в `NUMERIC(14,2)`, как 01.09.2026), откатится только
        она, а транзакция пачки останется рабочей. Решает, что делать с отказом,
        вызывающий — репозиторий исключение не глотает.
        """
        async with self._session.begin_nested():
            result = await self._session.execute(patch_statement(row, patch))
        return int(getattr(result, "rowcount", 0) or 0) == 1
