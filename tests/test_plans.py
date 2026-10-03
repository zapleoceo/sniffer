"""Тариф и зависимость, без которой он не работает.

Числа здесь не из памяти: период 2 592 000 проверен живым `createInvoiceLink`
01.09.2026 и подтверждён справочником Bot API; цена, потолки и «10 ⭐» — решения
владельца от 03.10.2026 (`docs/monetization.md`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import aiogram
from aiogram import Router

from sniffer.domain import plans

ROOT = Path(__file__).resolve().parents[1]


def test_the_tariff_is_ten_stars_for_thirty_days_in_the_stars_currency() -> None:
    assert plans.SUBSCRIPTION_STARS == 10
    assert plans.SUBSCRIPTION_CURRENCY == "XTR"
    assert plans.SUBSCRIPTION_PERIOD_S == 30 * 24 * 3600 == 2_592_000


def test_the_card_limits_are_the_owners_decision() -> None:
    assert (plans.FREE_CARDS_PER_PERIOD, plans.PAID_CARDS_CAP) == (10, 300)


def _aiogram_requirement() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found: list[str] = [
        dep for dep in project["project"]["dependencies"] if dep.startswith("aiogram")
    ]
    assert len(found) == 1, found
    return found[0]


def test_the_aiogram_floor_has_the_subscription_update() -> None:
    """Апдейт `subscription` (Bot API 10.2) появился в aiogram 3.30.

    Прод стоит на lock 3.31, но граница `>=3.13` ничего не гарантировала: свежая
    установка с неё взяла бы версию без хендлера отмен и сбоев подписки.
    """
    match = re.fullmatch(r"aiogram>=([0-9]+)\.([0-9]+)", _aiogram_requirement())
    assert match is not None, _aiogram_requirement()

    assert (int(match.group(1)), int(match.group(2))) >= (3, 30)


def test_the_lock_records_the_same_floor_as_pyproject() -> None:
    """`uv sync --frozen` не пересчитывает замок: расхождение там тихое."""
    spec = _aiogram_requirement().removeprefix("aiogram")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert f'{{ name = "aiogram", specifier = "{spec}" }}' in lock


def test_the_installed_aiogram_can_receive_subscription_updates() -> None:
    """Сама возможность, ради которой поднята граница, а не номер версии."""
    assert hasattr(Router(), "subscription")
    assert "subscription" in aiogram.types.Update.model_fields
    assert hasattr(aiogram.types, "BotSubscriptionUpdated")
