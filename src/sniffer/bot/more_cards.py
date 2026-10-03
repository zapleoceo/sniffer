"""«Ещё N» и «Показать все N»: продолжение выдачи по снимку.

Те же ворота, что у первой страницы (`showing.admitted`): страница, листаемая мимо
квоты, раздала бы платное бесплатно. Уже показанная карточка повторно не списывается —
это держит журнал (`Admission.repeated`), а курсор снимка не даёт показать её второй
раз глазам.

Курсор сдвигается ДО первого `await`: asyncio переключается только на ожидании, и два
быстрых нажатия на одну кнопку не пройдут оба проверку смещения. Сдвиг откатывается,
если страница так и не ушла, — иначе потерянная страница не вернулась бы никогда.
"""

from __future__ import annotations

from enum import StrEnum

from sniffer.bot.paging import SHOW_ALL_CAP, MoreOffer, Snapshot
from sniffer.bot.presenter import Gate, present_offer, present_page
from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.showing import Send, admitted, continuation, delivered, may_offer
from sniffer.config import get_settings
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD

ALL = "all"
MORE = "more"


class Result(StrEnum):
    SHOWN = "shown"
    # Кнопка не с того места: нажали дважды или нажали старую. Карточки уже на экране.
    STALE = "stale"
    # Страница не ушла (квота недоступна или Telegram отказал): можно нажать ещё раз.
    FAILED = "failed"


async def show_more(
    send: Send,
    token: str,
    snapshot: Snapshot,
    action: str,
    offset: int,
    *,
    quota: QuotaService,
    account: Account,
) -> Result:
    """Показать следующую страницу (`more`) или весь остаток до потолка (`all`)."""
    if offset != snapshot.cursor:
        return Result.STALE
    count = SHOW_ALL_CAP if action == ALL else get_settings().max_cards
    end = min(offset + count, len(snapshot.items))
    snapshot.cursor = end
    delivered_ok = False
    try:
        delivered_ok = await _page(send, token, snapshot, offset, end, quota=quota, account=account)
    finally:
        if not delivered_ok:
            snapshot.cursor = offset
    return Result.SHOWN if delivered_ok else Result.FAILED


async def _page(
    send: Send,
    token: str,
    snapshot: Snapshot,
    offset: int,
    end: int,
    *,
    quota: QuotaService,
    account: Account,
) -> bool:
    page = snapshot.items[offset:end]
    result = await admitted(send, quota, account, page, root=snapshot.root, request_id=None)
    if result is None:
        return False
    admission, shown = result
    total = len(snapshot.items)
    rest = total - end
    more = MoreOffer(token, end, rest) if continuation(admission, rest) else None
    try:
        offer = (
            admission.limit == FREE_CARDS_PER_PERIOD
            and bool(admission.withheld)
            and await may_offer(quota, account)
        )
        gate = Gate(admission=admission, shown=shown, offer=offer, more=more)
        replies = present_page(total, offset, gate, root=snapshot.root)
    except BaseException:
        # Резерв записан, до отправки не дошло: вернуть слоты, как при сбое отправки.
        await quota.release(admission)
        raise

    async def sending() -> None:
        for reply in replies:
            await send(reply)

    await delivered(quota, admission, sending)
    extra = present_offer(gate, root=snapshot.root)
    if extra is not None:
        await send(extra)
    return True
