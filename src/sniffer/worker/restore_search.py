"""Владелец возвращает убранный («Удалить поиск») поиск: `python -m sniffer.worker.restore_search`.

Только для аккаунта владельца (`Settings.is_owner`). Решение владельца: вернуть СУЩЕСТВУЮЩИЙ
поиск, а не заводить новый. Команда снимает пометку `archived` (связь становится `open`, если
сохранена тема, иначе `lost`: поиск снова в списках, тему ему пересоздаст штатный
`TabRepository.attach`) и включает его слежение на месте (`SlotRepository.resume_restored`).
Фильтр, версии паспорта и курсор не переписываются; пауза слежения не сбрасывается, так что
после суток простоя монитор сам поднимет курсор к «сейчас» и накопленное не придёт пачкой.

Только цепочка клиента, названного в аргументах; чужой и несуществующий корень — код 3 и ни одной
записи. Поиск не в архиве — ничего не делает (код 0), повтор безопасен. Оплаченное слежение
(`expires_at` задан) не возвращается: код 3, его включает «Следить» в боте. Ничего не отправляет:
карточки уйдут обычным нотифаером.

Коды: 0 — возвращён либо возвращать нечего, 2 — неверные аргументы, 3 — не владелец, поиск не
найден, чужой или оплаченный (одна строка причины в stderr).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.slots import PaidSearch, SlotRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.db.repositories.watch import WatchRepository

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND = 0, 2, 3


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="restore_search", description=__doc__)
    parser.add_argument("--tg-user-id", type=int, required=True)
    parser.add_argument("--root", type=int, required=True, help="корень цепочки паспорта")
    return parser.parse_args(list(argv))


def refuse(reason: str) -> int:
    """Код 3 с одной строкой причины: без Telegram-id и без текста поиска."""
    sys.stderr.write(f"restore_search: {reason}\n")
    return EXIT_NOT_FOUND


async def restore(args: argparse.Namespace, now: datetime) -> int:
    # Команда владельца: чужой слот она бесплатным не делает. Проверка первой — до базы.
    if not get_settings().is_owner(args.tg_user_id):
        return refuse("not the owner account")
    async with session_scope() as session:
        # Без `get_or_create`: незнакомый Telegram-id не должен рождать строку клиента.
        user = await UserRepository(session).get_by_tg_id(args.tg_user_id)
        if (
            user is None
            or user.id is None
            or not await DeliveryRepository(session).owns_chain(
                user_id=user.id, passport_root=args.root
            )
        ):
            return refuse("search not found for this client")
        # Порядок блокировок: вкладка, затем клиент и подписка; архив берёт подписку раньше
        # вкладки. Обе команды ручные, а `FOR UPDATE` держит только строку клиента, поэтому
        # перекрёстного ожидания нет.
        state = await WatchRepository(session).restore_archived(user.id, args.root)
        if state is None:
            sys.stdout.write(f"nothing to restore: root={args.root} is not archived\n")
            return EXIT_OK
        try:
            subscription = await SlotRepository(session).resume_restored(
                user.id, args.root, now=now
            )
        except PaidSearch as exc:
            # Без commit: возврат вкладки откатывается вместе с отказом.
            return refuse(str(exc))
        await session.commit()
    sys.stdout.write(f"restored root={args.root} tab={state} subscription={subscription}\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(restore(args, datetime.now(UTC)))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
