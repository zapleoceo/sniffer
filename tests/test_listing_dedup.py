"""План уборки: слияние дублей и переписывание ссылок (domain/listing_dedup.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sniffer.domain.listing_dedup import (
    ACTION_CANONICAL_FORM,
    ACTION_MERGE_INTO_TARGET,
    ACTION_NOT_PRIMARY,
    ACTION_POINTER_TARGET,
    CardKeys,
    plan_links,
    plan_merges,
)
from sniffer.domain.origin import Origin, OriginKind, PostRef

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def card(
    id_: int, hours: int, *, exact: str = "e", near: str | None = None, **kw: object
) -> CardKeys:
    base = CardKeys(
        id=id_,
        posted_at=T0 + timedelta(hours=hours),
        city="nha_trang",
        category="apartment",
        deal_type="rent_out",
        tg_link=f"https://t.me/chat{id_}/{id_}",
        exact=exact,
        near=near,
        price_amount=Decimal(6_000_000),
        price_currency="VND",
        price_period="month",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_the_earliest_post_stays_and_takes_the_newest_price() -> None:
    first = card(1, 0)
    repost = card(2, 48, price_amount=Decimal(5_500_000))
    middle = card(3, 24)

    (merge,) = plan_merges([repost, first, middle], near=False)

    assert merge.canonical_id == 1
    assert merge.duplicate_ids == (2, 3)
    assert merge.price_from_id == 2
    assert merge.posted_at == repost.posted_at


def test_an_unpriced_newest_copy_does_not_erase_the_price() -> None:
    first = card(1, 0)
    newer_priced = card(2, 10, price_amount=Decimal(7_000_000))
    newest_unpriced = card(3, 20, price_amount=None, price_currency=None, price_period=None)

    (merge,) = plan_merges([first, newer_priced, newest_unpriced], near=False)

    assert merge.price_from_id == 2


def test_the_same_price_is_not_rewritten() -> None:
    (merge,) = plan_merges([card(1, 0), card(2, 5)], near=False)

    assert merge.price_from_id is None


def test_a_private_link_never_wins_over_a_public_copy() -> None:
    """`t.me/c/…` открывается только участникам: клиенту это не первоисточник."""
    private = card(1, 0, tg_link="https://t.me/c/4307137181/1894")
    public = card(2, 5)

    (merge,) = plan_merges([private, public], near=False)

    assert merge.canonical_id == 2 and merge.duplicate_ids == (1,)


def test_another_city_or_deal_side_is_not_a_duplicate() -> None:
    cards = [card(1, 0), card(2, 1, city="da_nang"), card(3, 2, deal_type="sell")]

    assert plan_merges(cards, near=False) == []


def test_near_duplicates_merge_only_when_asked() -> None:
    cards = [card(1, 0, exact="a", near="n"), card(2, 5, exact="b", near="n")]

    assert plan_merges(cards, near=False) == []
    (merge,) = plan_merges(cards, near=True)
    assert merge.canonical_id == 1


def test_groups_are_joined_through_either_key() -> None:
    """A=B по тексту, B=C по ключу без цены — одно объявление из трёх копий."""
    cards = [
        card(1, 0, exact="x", near=None),
        card(2, 1, exact="x", near="n"),
        card(3, 2, exact="y", near="n"),
    ]

    (merge,) = plan_merges(cards, near=True)

    assert merge.canonical_id == 1 and merge.duplicate_ids == (2, 3)


def test_a_pointer_to_a_carded_original_merges_into_it() -> None:
    pointer = card(
        1,
        0,
        origin=Origin(OriginKind.POINTER, PostRef(9, username="arenda")),
    )

    (fix,) = plan_links([pointer], lambda post: ("https://t.me/arenda/9", 77))

    assert fix.action == ACTION_MERGE_INTO_TARGET and fix.target_listing_id == 77


def test_a_pointer_to_an_uncarded_original_gets_its_link() -> None:
    pointer = card(1, 0, origin=Origin(OriginKind.POINTER, PostRef(9, username="arenda")))

    (fix,) = plan_links([pointer], lambda post: ("https://t.me/arenda/9", None))

    assert (fix.action, fix.after) == (ACTION_POINTER_TARGET, "https://t.me/arenda/9")


def test_an_unresolvable_pointer_is_not_published_as_primary() -> None:
    shortener = card(1, 0, origin=Origin(OriginKind.POINTER))
    private = card(2, 0, origin=Origin(OriginKind.POINTER, PostRef(9, chat_tg_id=-1005)))

    fixes = plan_links([shortener, private], lambda post: (None, None))

    assert [fix.action for fix in fixes] == [ACTION_NOT_PRIMARY, ACTION_NOT_PRIMARY]


def test_a_mirror_link_is_written_in_one_form() -> None:
    mirror = card(1, 0, tg_link="http://telegram.me/s/Arenda/5?single")

    (fix,) = plan_links([mirror, card(2, 0)], lambda post: (None, None))

    assert (fix.action, fix.after) == (ACTION_CANONICAL_FORM, "https://t.me/Arenda/5")
