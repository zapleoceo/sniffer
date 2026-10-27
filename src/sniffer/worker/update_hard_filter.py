"""Правка жёсткого фильтра СУЩЕСТВУЮЩЕЙ подписки с проверкой (CAS): `update_hard_filter`.

В отличие от `grant_watch` (выдача: включает подписку и перезаписывает фильтр целиком) эта команда
меняет только `subscriptions.hard_filter` и только если сейчас там ровно то, что владелец ожидает
увидеть (`--expect-json`, `null` — фильтра нет). Новое значение (`--set-json`) проходит
`HardFilter.from_json`: опечатка отклоняется до базы. Ничего не отправляет, курсоры, срок и
активность не трогает.

Коды: 0 — применено (commit подтверждён и фильтр перечитан новым SELECT), 1 — неожиданный сбой,
2 — неверные аргументы или невалидный фильтр, 3 — подписки нет, 4 — конфликт (в базе другое
значение; оно печатается, ничего не записано), 5 — прервано, 6 — commit прошёл, но перечитанное
значение не совпало (успех не заявляется). На 1 и 5 результат не подтверждён: сверьте чтением.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from sniffer.db.engine import session_scope
from sniffer.db.repositories.slots import (
    SlotRepository,
    audit_hard_filter_update,
)
from sniffer.domain.hard_filter import HardFilter
from sniffer.runtime.exits import class_chain, is_interrupt, reraise_if_not_ours

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_CONFLICT = 0, 1, 2, 3, 4
EXIT_INTERRUPTED, EXIT_UNVERIFIED = 5, 6


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
    try:
        expected, new = build(args)
    except ValueError as exc:  # HardFilter.from_json или разбор JSON: до базы не доходим
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE
    wanted = new.to_json()
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
    # Commit подтверждён. «Применено» говорим только по настоящему чтению в новой сессии, и
    # только после него пишем аудит: эхо собственной записи ничего не доказывает.
    async with session_scope() as session:
        actual = await SlotRepository(session).read_hard_filter(args.subscription_id)
    shown = json.dumps(actual, ensure_ascii=False, sort_keys=True)
    if actual != wanted:
        sys.stdout.write(f"unverified subscription={args.subscription_id} readback={shown}\n")
        return EXIT_UNVERIFIED
    audit_hard_filter_update(
        args.subscription_id,
        before=expected.to_json() if expected is not None else None,
        after=actual,
    )
    sys.stdout.write(f"updated subscription={args.subscription_id} readback={shown}\n")
    return EXIT_OK


def answer(err: BaseException) -> int:
    """Документированный код на любую поломку; успех не изображается.

    Единственное, что не наше, — `SystemExit` (argparse: код 2 и прочие чужие) и `GeneratorExit`.
    Текст исключения не печатается: у базы он несёт параметры запроса (`class_chain`).
    """
    if isinstance(err, SystemExit):
        return err.code if isinstance(err.code, int) else EXIT_USAGE
    reraise_if_not_ours(err)
    if is_interrupt(err):
        sys.stderr.write("прервано: результат не подтверждён, сверьте фильтр чтением\n")
        return EXIT_INTERRUPTED
    sys.stderr.write(f"сбой: {class_chain(err)}; результат не подтверждён, сверьте фильтр\n")
    return EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    # Вся работа внутри охраны до корня иерархии; снаружи остаётся только вывод диагностики.
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(run(args))
    except BaseException as err:
        return answer(err)


if __name__ == "__main__":
    raise SystemExit(main())
