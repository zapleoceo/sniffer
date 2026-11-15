"""Уборка каталога: ссылки на первоисточник и слияние дублей.

    python -m sniffer.worker.primary_cleanup            # сухой прогон: план и числа
    python -m sniffer.worker.primary_cleanup --apply    # исполнить план
    python -m sniffer.worker.primary_cleanup --near     # + дубли с другой ценой

По умолчанию НИЧЕГО не пишет: печатает JSON-отчёт — сколько ссылок переписать,
сколько групп дублей, сколько карточек погасить, и до 10 примеров «до/после».
`--apply` исполняет тот же план; повторный прогон находит пустой план
(идемпотентность держат охранные условия в `db/repositories/primary_cleanup.py`).

Что делается (правила — `domain/listing_dedup.py`, решение — docs/primary-sources.md):

1. Ссылки. Карточка-указатель (текст — ссылка на чужой пост) получает ссылку на
   оригинал; если у оригинала уже есть своя карточка — гасится как дубль; если
   оригинал не определить (сокращатель, внешний сайт) — гасится как `not_primary`.
   Неканоническая запись t.me-ссылки приводится к одной форме.
2. Дубли. Одна группа — одно объявление: остаётся карточка первоисточника (самый
   ранний пост), цена — самая свежая, остальные гасятся с причиной
   `duplicate_merged`. Ничего не удаляется, снимок «как было» — в
   `listing_cleanup_events`.
3. Проверенный каталог: публикациям проставляются ключи личности, дубли
   помечаются `duplicate_of_*` и уходят из выдачи.

Коды: 0 — сделано (или сухой прогон); 1 — сбой; 2 — аргументы; 5 — прервано.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sniffer.db.engine import session_scope
from sniffer.db.repositories.primary_cleanup import PrimaryCleanupRepository
from sniffer.domain.fingerprint import fingerprint
from sniffer.domain.listing_dedup import (
    ACTION_CANONICAL_FORM,
    ACTION_MERGE_INTO_TARGET,
    ACTION_NOT_PRIMARY,
    ACTION_POINTER_TARGET,
    CardKeys,
    LinkFix,
    Merge,
    plan_links,
    plan_merges,
)
from sniffer.domain.listing_identity import identity_hash, identity_keys
from sniffer.domain.listing_state import REASON_DUPLICATE_MERGED
from sniffer.domain.origin import PostRef, pointer_origin

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 5
BATCH = 2000


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="primary_cleanup", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="исполнить план (иначе сухой прогон)")
    parser.add_argument("--near", action="store_true", help="сливать и перепосты с новой ценой")
    parser.add_argument("--samples", type=int, default=10, help="примеров до/после в отчёте")
    args = parser.parse_args(list(argv))
    if not 0 <= args.samples <= 100:
        parser.error("--samples: от 0 до 100")
    return args


def card_keys(row: dict[str, Any]) -> CardKeys:
    text = str(row["text"])
    return CardKeys(
        id=int(row["id"]),
        posted_at=row["posted_at"],
        city=row["city"],
        category=row["category"],
        deal_type=row["deal_type"],
        tg_link=row["tg_link"],
        exact=fingerprint(text),
        near=identity_hash(text),
        origin=pointer_origin(text),
        price_amount=row["price_amount"],
        price_currency=row["price_currency"],
        price_period=row["price_period"],
        price_usd_month=row["price_usd_month"],
    )


async def load_cards() -> list[CardKeys]:
    cards: list[CardKeys] = []
    after = 0
    while True:
        async with session_scope() as session:
            rows = await PrimaryCleanupRepository(session).cards(after_id=after, limit=BATCH)
        if not rows:
            return cards
        cards.extend(card_keys(row) for row in rows)
        after = int(rows[-1]["id"])


async def resolve(post: PostRef) -> tuple[str | None, int | None]:
    async with session_scope() as session:
        return await PrimaryCleanupRepository(session).resolve_post(post)


async def plan_link_fixes(cards: list[CardKeys]) -> list[LinkFix]:
    """`plan_links` синхронный, а разрешение адреса ходит в базу: сначала собираем ответы."""
    answers: dict[PostRef, tuple[str | None, int | None]] = {}
    for card in cards:
        post = card.origin.post if card.origin is not None else None
        if post is not None and post not in answers:
            answers[post] = await resolve(post)
    return plan_links(cards, lambda post: answers[post])


def publication_plan(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ключи каждой публикации и, для дублей, на кого она указывает."""
    first: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    ordered = sorted(rows, key=lambda row: (row["posted_at"] or "", row["fetched_at"]))
    plan = []
    for row in ordered:
        keys = list(
            identity_keys(row["url"], row["raw_text"] or "", textual=row["source"] != "chotot")
        )
        scope = (row["city"], row["category"], row["deal_type"])
        me = (row["source"], row["external_id"])
        duplicate_of = next((first[(*scope, key)] for key in keys if (*scope, key) in first), None)
        for key in keys:
            first.setdefault((*scope, key), me)
        plan.append({"key": me, "keys": keys, "duplicate_of": duplicate_of})
    return plan


def report(
    cards: list[CardKeys],
    fixes: list[LinkFix],
    merges: list[Merge],
    near_merges: list[Merge],
    publications: list[dict[str, Any]],
    samples: int,
) -> dict[str, Any]:
    by_id = {card.id: card for card in cards}
    actions = (
        ACTION_CANONICAL_FORM,
        ACTION_POINTER_TARGET,
        ACTION_MERGE_INTO_TARGET,
        ACTION_NOT_PRIMARY,
    )
    return {
        "cards_scanned": len(cards),
        "links": {action: sum(fix.action == action for fix in fixes) for action in actions},
        "duplicate_groups": len(merges),
        "cards_to_deactivate": sum(len(merge.duplicate_ids) for merge in merges),
        "price_updates": sum(merge.price_from_id is not None for merge in merges),
        "near_duplicate_groups_if_enabled": len(near_merges),
        "near_cards_to_deactivate_if_enabled": sum(len(m.duplicate_ids) for m in near_merges),
        "publications": len(publications),
        "publication_duplicates": sum(row["duplicate_of"] is not None for row in publications),
        "link_samples": [
            {
                "listing_id": fix.listing_id,
                "action": fix.action,
                "before": fix.before,
                "after": fix.after,
            }
            for fix in fixes[:samples]
        ],
        "merge_samples": [_merge_sample(merge, by_id) for merge in merges[:samples]],
    }


def _merge_sample(merge: Merge, by_id: dict[int, CardKeys]) -> dict[str, Any]:
    canonical = by_id[merge.canonical_id]
    donor = by_id.get(merge.price_from_id) if merge.price_from_id is not None else None
    return {
        "keep": {"listing_id": canonical.id, "tg_link": canonical.tg_link},
        "deactivate": [{"listing_id": i, "tg_link": by_id[i].tg_link} for i in merge.duplicate_ids],
        "price_before": _money(canonical),
        "price_after": _money(donor or canonical),
    }


def _money(card: CardKeys) -> str | None:
    if card.price_amount is None:
        return None
    period = f"/{card.price_period}" if card.price_period else ""
    return f"{card.price_amount} {card.price_currency or ''}{period}".strip()


async def apply(
    fixes: list[LinkFix], merges: list[Merge], publications: list[dict[str, Any]]
) -> dict[str, int]:
    done = {"links": 0, "merges": 0, "publications": 0}
    for fix in fixes:
        async with session_scope() as session:
            repo = PrimaryCleanupRepository(session)
            if fix.action in (ACTION_CANONICAL_FORM, ACTION_POINTER_TARGET) and fix.after:
                changed = await repo.rewrite_link(fix.listing_id, fix.before, fix.after, fix.action)
            elif fix.action == ACTION_MERGE_INTO_TARGET:
                changed = await repo.deactivate(
                    fix.listing_id, fix.target_listing_id, REASON_DUPLICATE_MERGED
                )
            else:
                changed = await repo.deactivate(fix.listing_id, None)
            await session.commit()
            done["links"] += int(changed)
    for merge in merges:
        async with session_scope() as session:
            merged = await PrimaryCleanupRepository(session).merge(
                merge.canonical_id, merge.duplicate_ids, merge.price_from_id, merge.posted_at
            )
            await session.commit()
            done["merges"] += int(merged)
    async with session_scope() as session:
        repo = PrimaryCleanupRepository(session)
        for row in publications:
            source, external_id = row["key"]
            await repo.set_publication_identity(
                source, external_id, row["keys"], row["duplicate_of"]
            )
        await session.commit()
        done["publications"] = len(publications)
    return done


async def plan(near: bool) -> tuple[list[CardKeys], list[LinkFix], list[Merge], list[Merge]]:
    cards = await load_cards()
    fixes = await plan_link_fixes(cards)
    # Указатели решаются в шаге ссылок; в группы дублей они не входят.
    pointers = {fix.listing_id for fix in fixes if fix.action != ACTION_CANONICAL_FORM}
    mergeable = [card for card in cards if card.id not in pointers]
    merges = plan_merges(mergeable, near=near)
    near_merges = merges if near else plan_merges(mergeable, near=True)
    return cards, fixes, merges, near_merges


async def execute(args: argparse.Namespace) -> int:
    cards, fixes, merges, near_merges = await plan(args.near)
    async with session_scope() as session:
        repo = PrimaryCleanupRepository(session)
        rebinds = await repo.earlier_copies()
        publications = publication_plan(await repo.publications())
    movable = [row for row in rebinds if not row["blocked"]]
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "near": args.near,
        "at": datetime.now(UTC).isoformat(),
        "earlier_copy": {
            "cards_to_rebind": len(movable),
            "of_them_same_chat_only": sum(bool(row["same_chat"]) for row in movable),
            "blocked_by_existing_card": len(rebinds) - len(movable),
            "samples": [
                {"listing_id": row["listing_id"], "before": row["before"], "after": _copy_url(row)}
                for row in movable[: args.samples]
            ],
        },
        **report(cards, fixes, merges, near_merges, publications, args.samples),
    }
    if args.apply:
        # Сначала ссылки (перенос на раннюю копию меняет, кто в группе первоисточник),
        # потом план слияний строится заново по уже исправленной базе.
        rebound = await apply_rebinds(movable)
        _, fixes, merges, _ = await plan(args.near)
        result["applied"] = {"earlier_copy": rebound, **await apply(fixes, merges, publications)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return EXIT_OK


def _copy_url(row: dict[str, Any]) -> str:
    return str(PostRef(int(row["msg_id"]), int(row["chat_tg_id"]), row["username"]).url)


async def apply_rebinds(rows: list[dict[str, Any]]) -> int:
    done = 0
    for row in rows:
        async with session_scope() as session:
            done += int(await PrimaryCleanupRepository(session).rebind(row, _copy_url(row)))
            await session.commit()
    return done


def is_interrupt(err: BaseException) -> bool:
    if isinstance(err, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(err, BaseExceptionGroup):
        return all(is_interrupt(sub) for sub in err.exceptions)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(execute(args))
    except BaseException as err:
        # Требование остановиться, не поломка: argparse выходит так с кодом 2 (EXIT_USAGE).
        if isinstance(err, SystemExit | GeneratorExit):
            raise
        if is_interrupt(err):
            sys.stderr.write("прервано; уже закоммиченные шаги остаются, повтор безопасен\n")
            return EXIT_INTERRUPTED
        sys.stderr.write(f"сбой: {type(err).__name__}\n")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
