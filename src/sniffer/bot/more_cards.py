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
        landed = await _page(send, token, snapshot, offset, end, quota=quota, account=account)
        delivered_ok = landed is not None
        if landed is not None:
            snapshot.cursor = landed
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
) -> int | None:
    """Страница ушла — вернуть, докуда дошёл курсор; `None` — не ушла.

    Курсор встаёт перед первой карточкой, которую квота удержала, а не в конец
    страницы: иначе удержанное «перепрыгивалось» бы и после подписки снимок
    продолжался бы мимо карточек, которых клиент так и не увидел.
    """
    page = snapshot.items[offset:end]
    result = await admitted(send, quota, account, page, root=snapshot.root, request_id=None)
    if result is None:
        return None
    admission, shown, ids = result
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

    gone: list[int] = []
    position = 0

    async def sending() -> None:
        nonlocal position
        for reply in replies:
            await send(reply)
            gone.extend(i for i in ids[position : position + reply.cards] if i is not None)
            position += reply.cards

    await delivered(quota, admission, sending, sent=lambda: gone)
    extra = present_offer(gate, root=snapshot.root)
    if extra is not None:
        await send(extra)
    admitted_now = {id(item) for item in shown}
    held = next((k for k, item in enumerate(page) if id(item) not in admitted_now), len(page))
    return offset + held
