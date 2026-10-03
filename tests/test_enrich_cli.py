"""Команда `python -m sniffer.worker enrich`: разбор аргументов и закрытый набор исходов.

Исходов четыре: 0 — проход закончился, 2 — неверные аргументы, 130 — прервали,
1 — всё остальное. Полнота этого набора держится построением (CLAUDE.md, «Как
закрывают набор кодов возврата»): охрана каждого шага стоит до КОРНЯ иерархии,
а проверяется она чужим типом по обеим осям — `Exception` и `BaseException` —
и сверкой числа охран в исходнике с числом шагов в матрице.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Sequence

import pytest

from sniffer.worker import __main__ as worker_main
from sniffer.worker import enrich_cli
from sniffer.worker.enrich_cli import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    Options,
    UsageError,
    parse_options,
    run_enrich,
)
from sniffer.worker.enrich_report import END, EnrichReport

PHONE = "+84 90 123 45 67"


class Boom(Exception):
    """Чужой тип: код его не знает и не перечисляет."""


class Output:
    """Куда команда говорит: два канала, как stdout и stderr."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def command(self, argv: list[str], runner: enrich_cli.Runner | None = None) -> int:
        return run_enrich(
            argv, runner=runner or finishing, out=self.out.append, err=self.err.append
        )

    @property
    def said(self) -> str:
        return "\n".join(self.out + self.err)


async def finishing(options: Options, report: EnrichReport) -> None:
    """Проход, который дошёл до конца и ничего не нашёл."""
    report.dry_run = options.dry_run
    report.stop_reason = END


# ── структура: ПЕРВЫМ, чтобы красное успело появиться до обрыва прогона ──

# Каждый шаг, который делает работу: разбор аргументов и сам проход. Ломается шаг
# так: подменой функции, которую он зовёт.
STEPS = ("parse", "run")


def test_every_guarded_step_of_run_enrich_is_in_the_matrix() -> None:
    """Матрица шагов — тоже список того, что вспомнили; сверяем с устройством.

    Шагов в команде ровно столько, сколько охран до корня в её исходнике, и
    ни одной охраны, которая кончается на `Exception`.
    """
    source = inspect.getsource(run_enrich)

    assert source.count("except BaseException") == len(STEPS)
    assert "except Exception" not in source
    assert "except:" not in source


# ── аргументы ──────────────────────────────────────────────────────────────


def test_no_arguments_means_a_live_pass_over_everything() -> None:
    assert parse_options([]) == Options(dry_run=False, limit=None, since_id=0)


def test_all_the_flags_together() -> None:
    options = parse_options(["--dry-run", "--limit", "500", "--since-id", "1200"])

    assert options == Options(dry_run=True, limit=500, since_id=1200)


def test_a_value_may_be_glued_with_an_equals_sign() -> None:
    assert parse_options(["--limit=5", "--since-id=7"]) == Options(limit=5, since_id=7)


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (["--bogus"], "--bogus"),
        (["enrich"], "enrich"),
        (["--limit"], "--limit"),
        (["--limit", "abc"], "abc"),
        (["--limit", "0"], "--limit"),
        (["--limit", "-3"], "--limit"),
        (["--since-id", "-1"], "--since-id"),
        (["--since-id", "x"], "--since-id"),
        (["--limit", "1", "--limit", "2"], "--limit"),
        (["--dry-run", "--dry-run"], "--dry-run"),
        (["--dry-run=1"], "--dry-run"),
        (["--dry"], "--dry"),
    ],
)
def test_a_wrong_argument_is_a_usage_error_that_names_it(argv: list[str], why: str) -> None:
    with pytest.raises(UsageError, match=re.escape(why)):
        parse_options(argv)


def test_an_id_beyond_the_range_of_the_database_is_a_usage_error_not_a_driver_error() -> None:
    with pytest.raises(UsageError, match="--since-id"):
        parse_options(["--since-id", str(2**63)])

    assert parse_options(["--since-id", str(2**63 - 1)]).since_id == 2**63 - 1


def test_a_wrong_argument_answers_with_the_usage_code_and_never_starts_the_pass() -> None:
    said = Output()
    started: list[str] = []

    async def runner(options: Options, report: EnrichReport) -> None:
        started.append("запущен")

    assert said.command(["--limit", "abc"], runner) == EXIT_USAGE
    assert started == []
    assert "--limit" in said.said and "Использование" in said.said


def test_help_prints_the_usage_and_is_a_success() -> None:
    said = Output()

    assert said.command(["--help"]) == EXIT_OK
    assert "Использование" in said.said and "--dry-run" in said.said


# ── успех ──────────────────────────────────────────────────────────────────


def test_a_finished_pass_prints_the_report_and_answers_zero() -> None:
    said = Output()

    code = said.command(["--dry-run"])

    assert code == EXIT_OK
    assert "dry-run" in said.said and "НИЧЕГО не записано" in said.said
    assert said.err == []


def test_the_runner_receives_the_parsed_options() -> None:
    received: list[Options] = []

    async def runner(options: Options, report: EnrichReport) -> None:
        received.append(options)

    Output().command(["--dry-run", "--limit", "3"], runner)

    assert received == [Options(dry_run=True, limit=3, since_id=0)]


def test_a_dead_output_channel_does_not_turn_a_finished_pass_into_a_failure() -> None:
    """`… | head -1` закрывает stdout: проход уже сделал дело, трейсбек тут ложь."""

    def dead(_text: str) -> None:
        raise BrokenPipeError

    assert run_enrich([], runner=finishing, out=dead, err=dead) == EXIT_OK


# ── закрытый набор исходов ─────────────────────────────────────────────────


def _async_raiser(failure: BaseException) -> enrich_cli.Runner:
    async def runner(options: Options, report: EnrichReport) -> None:
        raise failure

    return runner


def break_step(
    step: str, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> enrich_cli.Runner:
    """Ломает шаг и отдаёт исполнитель для команды.

    Заглушка принимает ЛЮБЫЕ аргументы: иначе `TypeError` скроет подложенное
    исключение, шаг ответит документированным кодом, и тест будет зелёным,
    не проверив ничего.
    """

    def raiser(*_args: object, **_kwargs: object) -> object:
        raise failure

    if step == "parse":
        monkeypatch.setattr(enrich_cli, "parse_options", raiser)
        return finishing
    return _async_raiser(failure)


@pytest.mark.parametrize("step", STEPS)
def test_every_step_answers_with_a_documented_code_for_an_unknown_failure(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = Output()
    runner = break_step(step, monkeypatch, Boom(f"INSERT … title='{PHONE}'"))

    code = said.command([], runner)

    assert code == EXIT_FAILED
    assert "Boom" in said.said, "класс сбоя назван"
    assert PHONE not in said.said, "текст сбоя не печатается: в нём параметры SQL"


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        BaseExceptionGroup("g", [KeyboardInterrupt(), asyncio.CancelledError()]),
    ],
    ids=["ctrl-c", "cancelled", "group"],
)
def test_an_interrupt_at_any_step_is_words_not_a_traceback(
    step: str, interrupt: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = Output()
    runner = break_step(step, monkeypatch, interrupt)

    assert said.command([], runner) == EXIT_INTERRUPTED
    assert "Прервано" in said.said


@pytest.mark.parametrize("step", STEPS)
def test_a_mixed_group_answers_about_the_failure_not_the_interrupt(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = Output()
    runner = break_step(step, monkeypatch, BaseExceptionGroup("g", [KeyboardInterrupt(), Boom()]))

    assert said.command([], runner) == EXIT_FAILED


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("request_to_stop", [SystemExit(7), GeneratorExit()])
def test_a_request_to_stop_is_passed_through_not_rewritten(
    step: str, request_to_stop: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = break_step(step, monkeypatch, request_to_stop)

    with pytest.raises(type(request_to_stop)) as caught:
        Output().command([], runner)

    assert caught.value is request_to_stop


def test_the_documented_codes_are_exactly_the_modules_constants() -> None:
    codes = {
        name: value
        for name, value in vars(enrich_cli).items()
        if re.fullmatch(r"EXIT_[A-Z_]+", name)
    }

    assert codes == {"EXIT_OK": 0, "EXIT_FAILED": 1, "EXIT_USAGE": 2, "EXIT_INTERRUPTED": 130}


def test_a_failure_still_prints_how_far_the_pass_got() -> None:
    said = Output()

    async def runner(options: Options, report: EnrichReport) -> None:
        report.last_id = 4200
        raise Boom("база пропала")

    code = said.command([], runner)

    assert code == EXIT_FAILED
    assert "--since-id 4200" in said.said, "оператору нужно знать, с чего продолжать"


def test_a_failure_in_a_dry_run_says_that_nothing_was_written() -> None:
    said = Output()

    said.command(["--dry-run"], _async_raiser(Boom("x")))

    assert "Сухой прогон ничего не записывал" in said.said


def test_a_failure_in_a_live_run_says_that_what_was_written_stays() -> None:
    said = Output()

    said.command([], _async_raiser(Boom("x")))

    assert "Записанное остаётся записанным" in said.said
    assert "Сухой прогон" not in said.said


def test_a_dead_output_channel_does_not_change_the_failure_code() -> None:
    def dead(_text: str) -> None:
        raise OSError("канал закрыт")

    assert run_enrich([], runner=_async_raiser(Boom("x")), out=dead, err=dead) == EXIT_FAILED


# ── сборка настоящего прохода ──────────────────────────────────────────────


async def test_the_real_runner_starts_the_pass_with_the_options_and_always_closes_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakePass:
        async def run(self, report: EnrichReport, **kwargs: object) -> None:
            calls.append(kwargs)
            raise Boom("сбой посреди прохода")

    async def fake_dispose() -> None:
        calls.append("пул закрыт")

    monkeypatch.setattr(enrich_cli, "EnrichPass", FakePass)
    monkeypatch.setattr(enrich_cli, "dispose_engine", fake_dispose)
    monkeypatch.setattr(enrich_cli, "setup_logging", lambda _level: None)

    with pytest.raises(Boom):
        await enrich_cli.run_pass(Options(dry_run=True, limit=9, since_id=3), EnrichReport())

    assert calls == [{"dry_run": True, "limit": 9, "since_id": 3}, "пул закрыт"]


# ── точка входа процесса ───────────────────────────────────────────────────


def test_without_arguments_the_worker_starts_as_a_service(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(worker_main, "run_service", started.append)

    assert worker_main.main([]) == 0
    assert started == [worker_main.SERVICE]


def test_the_enrich_subcommand_goes_to_the_command_with_the_rest_of_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str]] = []

    def fake_command(argv: Sequence[str]) -> int:
        received.append(list(argv))
        return 5

    monkeypatch.setattr(worker_main, "run_enrich", fake_command)
    monkeypatch.setattr(worker_main, "run_service", pytest.fail)

    assert worker_main.main(["enrich", "--dry-run"]) == 5
    assert received == [["--dry-run"]]


@pytest.mark.parametrize("argv", [["enrch"], ["--dry-run"], ["auth"]])
def test_an_unknown_subcommand_never_silently_starts_the_daemon(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Опечатка в подкоманде не повод поднять демона: владелец ждал бы отчёт, которого не будет."""
    monkeypatch.setattr(worker_main, "run_service", pytest.fail)

    assert worker_main.main(argv) == EXIT_USAGE
    assert "enrich" in capsys.readouterr().err
