"""Ручная выдача слежения без платежа: `python -m sniffer.worker.grant_watch`.

Нужна владельцу: оплаченного слота у него нет, а «Следить» без слота не включается. Команда
заводит (или возобновляет) мониторинг без срока (`SlotRepository.grant`) и кладёт на него жёсткие
условия (`domain.hard_filter`). Ничего не отправляет: первую карточку, если она найдётся, пришлёт
обычный нотифаер тому же клиенту, чей это поиск.

Коды: 0 — выдано, 2 — неверные аргументы, 3 — поиск не найден или чужой.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.slots import SlotRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.hard_filter import HardFilter

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND = 0, 2, 3


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="grant_watch", description=__doc__)
    parser.add_argument("--tg-user-id", type=int, required=True)
    parser.add_argument("--root", type=int, required=True, help="корень цепочки паспорта")
    parser.add_argument("--require", action="append", default=[], help="balcony | separate_kitchen")
    parser.add_argument("--district", action="append", default=[], help="слаг района справочника")
    parser.add_argument("--place-word", action="append", default=[], help="слово-адрес в тексте")
    parser.add_argument("--lookback-hours", type=float, default=0.0)
    return parser.parse_args(list(argv))


async def grant(args: argparse.Namespace, now: datetime) -> int:
    spec = HardFilter.from_json(
        {"require": args.require, "districts": args.district, "place_words": args.place_word}
    )
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(args.tg_user_id)
        if user.id is None or not await DeliveryRepository(session).owns_chain(
            user_id=user.id, passport_root=args.root
        ):
            return EXIT_NOT_FOUND
        subscription = await SlotRepository(session).grant(
            user.id,
            args.root,
            hard_filter=spec,
            lookback=timedelta(hours=args.lookback_hours),
            now=now,
        )
        await session.commit()
    sys.stdout.write(f"subscription={subscription} root={args.root} filter={spec}\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(grant(args, datetime.now(UTC)))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    except ValueError as exc:  # HardFilter.from_json: опечатка в условии
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
