"""Per-recipient quota for deferred answers: the share of a shared analysis one person may see.

The analysis is done once per collection task for every subscriber of that scope, but the
allowance is personal: one recipient has ten free cards left, another has none. This
module cuts the shared cards down to what *this* recipient may see, through the same
`QuotaService.admit` the chat uses, so the rule lives in one place.

It only decides and reserves. Turning the decision into words and into the outbox payload
is `followup.py`'s job, and confirming or handing the reservation back is too: only it
knows whether the reply really entered the outbox.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.showing import listing_ids
from sniffer.db.repositories.collection_tasks import CollectionRecipient
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD
from sniffer.domain.quota import Admission, Channel
from sniffer.sources.base import RawItem


@dataclass(frozen=True, slots=True)
class FoundCards:
    """What the analysis found, apart from its rendering: a quota re-cuts it per person."""

    prefix: str
    items: list[RawItem]
    total: int


@dataclass(frozen=True, slots=True)
class Cut:
    """One recipient's share: the cards they may see, the reservation behind it, the offer right."""

    shown: list[RawItem]
    admission: Admission
    offer: bool


async def cut_for(
    quota: QuotaService,
    session: AsyncSession,
    recipient: CollectionRecipient,
    found: FoundCards,
) -> Cut | None:
    """Admit the recipient's share of the found cards. `None` - no such client, nothing to meter."""
    user = await UserRepository(session).get(recipient.user_id)
    if user is None:  # pragma: no cover - the recipient row references users
        return None
    account = Account(user_id=recipient.user_id, tg_user_id=user.tg_user_id)
    ids = await listing_ids(quota, found.items)
    admission = await quota.admit(
        account,
        [listing for listing in ids if listing is not None],
        Channel.DEFERRED,
        # The request id of a recipient is the root of its passport chain.
        passport_root=recipient.request_id,
    )
    shown = [
        item
        for item, listing in zip(found.items, ids, strict=True)
        if listing is None or admission.allows(listing)
    ]
    offer = (
        admission.limit == FREE_CARDS_PER_PERIOD
        and bool(admission.withheld)
        and await quota.may_offer(account)
    )
    return Cut(shown=shown, admission=admission, offer=offer)
