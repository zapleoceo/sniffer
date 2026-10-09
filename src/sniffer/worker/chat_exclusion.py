"""Исключение группы из сбора и возврат: `python -m sniffer.worker.chat_exclusion`.

    exclude --tg-id N --reason zero_useful --evidence-json '<json>' [--dry-run]
    restore --tg-id N --reason <почему> [--dry-run]

Решение владельца, а не эвристика: «группа с доказанным нулём пользы» выводится из сбора
обратимо. Строка чата остаётся (курсоры, имя, история), ставится `is_active=false` и
`excluded_*`, в журнал `chat_exclusion_events` пишется событие — одной транзакцией. В
Telegram команда не ходит вовсе: из группы мы не выходим, ничего не отправляем.

`--evidence-json` — снимок доказательств (период покрытия, число сообщений, прошедших гейт,
полезных предложений, непроверенных, backfill_done, backfill_msg_id/last_msg_id, coverage).
Он хранится в самой записи, а не вычисляется по `raw_messages`: сырьё без карточки чистится
через 90 дней, и пропавшие строки потом читались бы как ноль. Курсоры чата и дата снимка
(`snapshot_at`) дописываются сами, если их не передали.

Коды: 0 — сделано (или `--dry-run`: что было бы сделано, ничего не записано);
1 — неожиданный сбой; 2 — неверные аргументы; 3 — такого чата нет в реестре;
4 — чат уже в запрошенном состоянии (повтор безопасен, второго события нет);
5 — прервано (Ctrl+C, снятая задача).

Охрана: весь ход — внутри одного блока, чей последний `except` — `BaseException`
(CLAUDE.md, «Как закрывают набор кодов возврата»). Перечня ожидаемых исключений здесь нет
нарочно: полноту доказывает тест с чужим типом, а не список.
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
from sniffer.db.repositories.chat_exclusion import EXCLUDE, ChatExclusionRepository, Outcome
from sniffer.db.repositories.chats import ChatRepository

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_NOOP, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 5
DEFAULT_ACTOR = "cli:chat_exclusion"


class UsageError(ValueError):
    """Неверный аргумент, который argparse не поймал сам (содержимое JSON)."""


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="chat_exclusion", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in (EXCLUDE, "restore"):
        one = sub.add_parser(name)
        one.add_argument("--tg-id", type=int, required=True)
        one.add_argument("--reason", required=True)
        one.add_argument("--actor", default=DEFAULT_ACTOR)
        one.add_argument("--dry-run", action="store_true", help="ничего не писать")
        if name == EXCLUDE:
            one.add_argument("--evidence-json", required=True, help="снимок доказательств")
    return parser.parse_args(list(argv))


def evidence_from(raw: str, now: datetime) -> dict[str, Any]:
    """Снимок доказательств: непустой JSON-объект. Пустое «доказательство» — не решение."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--evidence-json не JSON: {exc.msg}") from exc
    if not isinstance(value, dict) or not value:
        raise UsageError("--evidence-json должен быть непустым JSON-объектом")
    return {"snapshot_at": now.isoformat(), **value}


def is_interrupt(err: BaseException) -> bool:
    """Ctrl+C, снятая задача — или ГРУППА только из таких (`TaskGroup` прячет их в группу)."""
    if isinstance(err, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(err, BaseExceptionGroup):
        return all(is_interrupt(sub) for sub in err.exceptions)
    return False


def let_exit_through(err: BaseException) -> None:
    """`SystemExit` и `GeneratorExit` — требование остановиться, чужой код выхода не меняем."""
    if isinstance(err, SystemExit | GeneratorExit):
        raise err


async def execute(args: argparse.Namespace, now: datetime) -> int:
    evidence = evidence_from(args.evidence_json, now) if args.action == EXCLUDE else {}
    async with session_scope() as session:
        chat = await ChatRepository(session).get_by_tg_id(args.tg_id)
        if chat is None:
            sys.stdout.write(f"tg_id={args.tg_id}: в реестре нет\n")
            return EXIT_NOT_FOUND
        excluded = chat.excluded_at is not None
        if excluded == (args.action == EXCLUDE):
            state = "уже исключён" if excluded else "не исключён"
            sys.stdout.write(f"tg_id={args.tg_id} ({chat.title}): {state}, ничего не меняю\n")
            return EXIT_NOOP
        if args.dry_run:
            sys.stdout.write(
                f"dry-run: {args.action} tg_id={args.tg_id} ({chat.title}) "
                f"reason={args.reason} evidence={json.dumps(evidence, ensure_ascii=False)}; "
                "ничего не записано\n"
            )
            return EXIT_OK
        repo = ChatExclusionRepository(session)
        if args.action == EXCLUDE:
            outcome = await repo.exclude(
                args.tg_id, reason=args.reason, evidence=evidence, actor=args.actor, now=now
            )
        else:
            outcome = await repo.restore(args.tg_id, reason=args.reason, actor=args.actor, now=now)
        if outcome is Outcome.DONE:
            await session.commit()  # статус чата и событие журнала — одна транзакция
            sys.stdout.write(f"tg_id={args.tg_id} ({chat.title}): {args.action} выполнен\n")
            return EXIT_OK
        # Гонка: между чтением и блокировкой строки её успел изменить другой процесс.
        sys.stdout.write(f"tg_id={args.tg_id}: {outcome.value}, ничего не меняю\n")
        return EXIT_NOOP if outcome is Outcome.NOOP else EXIT_NOT_FOUND


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        return asyncio.run(execute(args, datetime.now(UTC)))
    except UsageError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE
    except BaseException as err:
        let_exit_through(err)
        if is_interrupt(err):
            sys.stderr.write("прервано, ничего не записано\n")
            return EXIT_INTERRUPTED
        # Тип без текста: в сообщениях драйверов бывают адреса и параметры подключения.
        sys.stderr.write(f"сбой: {type(err).__name__}\n")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
