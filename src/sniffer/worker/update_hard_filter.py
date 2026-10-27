"""Правка жёсткого фильтра СУЩЕСТВУЮЩЕЙ подписки с проверкой (CAS): `update_hard_filter`.

В отличие от `grant_watch` (выдача: включает подписку и перезаписывает фильтр целиком) эта команда
меняет только `subscriptions.hard_filter` и только если сейчас там ровно то, что владелец ожидает
увидеть (`--expect-json`, `null` — фильтра нет). Новое значение (`--set-json`) проходит
`HardFilter.from_json`: опечатка отклоняется до базы. Ничего не отправляет, курсоры, срок и
активность не трогает.

Коды: 0 — применено, 2 — неверные аргументы или невалидный фильтр, 3 — подписки нет,
4 — конфликт (в базе другое значение; оно печатается, ничего не записано).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from sniffer.db.engine import session_scope
from sniffer.db.repositories.slots import SlotRepository
from sniffer.domain.hard_filter import HardFilter

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_CONFLICT = 0, 2, 3, 4


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="update_hard_filter", description=__doc__)
    parser.add_argument("--subscription-id", type=int, required=True)
    parser.add_argument("--expect-json", required=True, help="ожидаемый фильтр или null")
    parser.add_argument("--set-json", required=True, help="новый фильтр (объект)")
    return parser.parse_args(list(argv))


def _load(raw: str, *, allow_null: bool) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"не JSON: {exc.msg}") from exc
    if value is None and allow_null:
        return None
    if not isinstance(value, dict):
        raise ValueError("ожидается JSON-объект")
    return value


def build(args: argparse.Namespace) -> tuple[HardFilter | None, HardFilter]:
    """Оба значения через `HardFilter.from_json`; `ValueError` — невалидное."""
    expected = HardFilter.from_json(_load(args.expect_json, allow_null=True))
    new = HardFilter.from_json(_load(args.set_json, allow_null=False))
    assert new is not None
    return expected, new


async def run(args: argparse.Namespace) -> int:
    expected, new = build(args)
    async with session_scope() as session:
        try:
            result = await SlotRepository(session).update_hard_filter(
                args.subscription_id, expected=expected, new=new
            )
        except LookupError as exc:
            sys.stderr.write(f"{exc}\n")
            return EXIT_NOT_FOUND
        if not result.applied:
            await session.rollback()
            current = json.dumps(result.current, ensure_ascii=False, sort_keys=True)
            sys.stdout.write(f"conflict subscription={args.subscription_id} current={current}\n")
            return EXIT_CONFLICT
        await session.commit()
    sys.stdout.write(f"updated subscription={args.subscription_id} filter={new.to_json()}\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(run(args))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    except ValueError as exc:  # HardFilter.from_json или разбор JSON
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
