"""Выдача через квоту: допуск → отправка → подтверждение или возврат.

Единственное место, где найденное встречается с лимитом. `Conversation` отдаёт сюда
итог поиска, а не решает, сколько и что показать: это правило продукта, а не порядок
шагов диалога. Презентер (`presenter.py`) остаётся чистой функцией — он получает
готовое решение (`Gate`) и рисует его, а решает и пишет в журнал этот модуль.

Порядок обязан быть именно таким. Резерв записывается и коммитится ДО отправки:
двойное нажатие запускает два поиска сразу, и «показал — потом записал» дало бы
обоим одни и те же карточки. Не отправилось — `release`, слоты возвращаются. Отправилось
— `confirm`. Предложение подписки уходит после выдачи и отдельным сообщением: оно
не должно задерживать карточки и не должно терять их при своём сбое.

Страница — первые `MAX_CARDS` находок, как и без квоты: квота решает, какие из них
пустить, а не сколько их в странице. Находки, которых нет в журнале (источник не
передал `listing_id`, а по паре «источник, внешний id» карточки не нашлось), показываем,
но не считаем и пишем об этом в лог: прятать выдачу молча хуже, чем не посчитать.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence

import structlog

from sniffer.bot import wording_plan
from sniffer.bot.paging import SNAPSHOTS, MoreOffer, Snapshot, SnapshotStore, diversify
from sniffer.bot.presenter import Gate, Reply, Results, present, present_offer
from sniffer.bot.quota import Account, QuotaService
from sniffer.config import get_settings
from sniffer.domain.passport import Passport
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD
from sniffer.domain.quota import Admission, Channel
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)

Send = Callable[[Reply], Awaitable[None]]


def _carried(item: RawItem) -> int | None:
    """`listing_id`, с которым находку отдал сам источник (архивный каталог)."""
    value = item.raw.get("listing_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def listing_ids(quota: QuotaService, items: Sequence[RawItem]) -> list[int | None]:
    """Карточка журнала для каждой находки; `None` — опознать не удалось.

    Сначала то, что принёс источник; остальное — по паре «источник, внешний id»: живая
    находка записывается в `listings` до показа, и пара её находит.
    """
    ids = [_carried(item) for item in items]
    missing = [
        (item.source, item.external_id)
        for item, known in zip(items, ids, strict=True)
        if known is None
    ]
    if missing:
        found = await quota.identify(missing)
        ids = [
            known if known is not None else found.get((item.source, item.external_id))
            for item, known in zip(items, ids, strict=True)
        ]
    unidentified = [item.source for item, known in zip(items, ids, strict=True) if known is None]
    if unidentified:
        log.warning(
            "quota.unidentified_cards", count=len(unidentified), sources=sorted(set(unidentified))
        )
    return ids


async def may_offer(quota: QuotaService, account: Account) -> bool:
    """Предложение — довесок: его сбой не должен отнимать у человека карточки."""
    try:
        return await quota.may_offer(account)
    except Exception as exc:
        log.warning("quota.offer_claim_failed", kind=type(exc).__name__, error=str(exc))
        return False


async def admitted(
    send: Send,
    quota: QuotaService,
    account: Account,
    page: Sequence[RawItem],
    *,
    root: int | None,
    request_id: int | None,
) -> tuple[Admission, tuple[RawItem, ...], tuple[int | None, ...]] | None:
    """Допуск страницы: что из неё положено показать. `None` — квота недоступна, ответ уже ушёл.

    Одна и та же дорога для первой страницы и для «Ещё»: страница, листаемая в обход квоты,
    раздала бы платное бесплатно, а повторно показанная карточка не должна списываться
    второй раз — это держит журнал (`Admission.repeated`), а не вызывающий.
    """
    try:
        ids = await listing_ids(quota, page)
        admission = await quota.admit(
            account,
            [listing for listing in ids if listing is not None],
            Channel.SEARCH,
            passport_root=root,
            request_id=request_id,
        )
    except Exception:
        # Граница показа: недоступная квота не должна оставлять клиента без ответа, а
        # показать карточки в обход лимита значило бы раздать их бесплатно. Честная
        # реплика — единственное, что можно сделать, не зная остатка.
        log.exception("quota.admit_failed", user_id=account.user_id)
        await send(Reply(wording_plan.QUOTA_UNAVAILABLE))
        return None
    allowed = [
        (item, listing)
        for item, listing in zip(page, ids, strict=True)
        if listing is None or admission.allows(listing)
    ]
    # Идентификаторы идут параллельно `shown`: по ним при частичной доставке видно, какие
    # карточки клиент уже получил.
    return admission, tuple(item for item, _ in allowed), tuple(listing for _, listing in allowed)


async def delivered(
    quota: QuotaService,
    admission: Admission,
    sending: Callable[[], Awaitable[None]],
    *,
    sent: Callable[[], Collection[int]] | None = None,
) -> None:
    """Отправка под резервом: ушло — `confirm`, не ушло — `release`, и ошибка идёт дальше.

    `sent` — какие карточки уже ушли к моменту сбоя (страница из нескольких сообщений):
    они остаются показом, возвращаются только недоставленные.
    """
    try:
        await sending()
    except BaseException:
        # Сообщение не ушло (или ушло неизвестно что): слоты, которые этот допуск занял,
        # возвращаются. Из корня иерархии, а не из `Exception`: отмена задачи по таймауту
        # тоже означает «не показано».
        already = sent() if sent is not None else ()
        if already:
            await quota.settle_partial(admission, already)
        else:
            await quota.release(admission)
        raise
    await quota.confirm(admission)


def continuation(admission: Admission, rest: int) -> bool:
    """Есть ли смысл звать «Ещё»: остаток карточек есть, квота их не отрезала и не пуста."""
    if rest <= 0 or admission.withheld:
        return False
    return admission.remaining is None or admission.remaining > 0


async def show(
    send: Send,
    passport: Passport,
    found: Results,
    *,
    root: int | None,
    quota: QuotaService | None = None,
    account: Account | None = None,
    request_id: int | None = None,
    snapshots: SnapshotStore | None = None,
) -> None:
    """Показать итог поиска. Без квоты или без аккаунта — как раньше, без ограничений."""
    if quota is None or account is None or not found.items:
        await send(present(passport, found, root=root))
        return
    ordered = tuple(diversify(found.items))
    size = get_settings().max_cards
    result = await admitted(send, quota, account, ordered[:size], root=root, request_id=request_id)
    if result is None:
        return
    admission, shown, _ids = result
    more: MoreOffer | None = None
    rest = len(ordered) - size
    if continuation(admission, rest):
        store = SNAPSHOTS if snapshots is None else snapshots
        snapshot = Snapshot(account.tg_user_id, ordered, root, cursor=size)
        more = MoreOffer(store.put(snapshot), size, rest)
    try:
        offer = (
            admission.limit == FREE_CARDS_PER_PERIOD
            and bool(admission.withheld)
            and await may_offer(quota, account)
        )
        gate = Gate(admission=admission, shown=shown, offer=offer, more=more)
        reply = present(passport, found, root=root, gate=gate)
    except BaseException:
        # Резерв уже записан, а до отправки дело не дошло: вернуть, как и при сбое отправки.
        await quota.release(admission)
        raise

    async def sending() -> None:
        await send(reply)

    await delivered(quota, admission, sending)
    extra = present_offer(gate, root=root)
    if extra is not None:
        await send(extra)
