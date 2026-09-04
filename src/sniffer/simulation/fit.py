"""Мерка: чем показанный лот противоречит запросу.

Живёт отдельно от каталога намеренно. Каталог — это подделка источника, мерка —
эталон, которым подделку и всё остальное судят. Держи их в одном файле, и
«подчистить каталог, чтобы отчёт выглядел лучше» стало бы неотличимо от
«поправить мерку»: обе правки в одном месте, обе двигают цифру вниз.

И вторая причина, важнее первой: мерка НЕ пользуется формулой ранжировщика.
Считать релевантность его же баллом значит объявить его верным по определению —
любая ошибка отбора превратилась бы в «так и задумано». Здесь сравнивается
правда о лоте (`Lot`) с тем, что клиент назвал в паспорте, и больше ничего.
"""

from __future__ import annotations

from sniffer.domain.passport import Budget, Currency, Intent, Passport
from sniffer.simulation.catalog import Lot

# Курс зафиксирован намеренно: живой `usd_vnd_rate()` ходит в сеть, а отчёт,
# который меняется от чужого курса, сравнить со вчерашним нельзя.
USD_VND = 25_000.0

# Насколько объём двигателя вправе разойтись с названным, оставаясь тем же
# запросом. «200 кубиков» — это про класс мотоцикла, а не про точное число:
# 175 и 250 клиент назовёт тем же поиском, 700 — уже нет.
ENGINE_CC_TOLERANCE = 0.25


def off_target(lot: Lot, passport: Passport) -> str:
    """Чем этот лот противоречит запросу. Пустая строка — не противоречит."""
    if passport.category is not None and lot.category is not passport.category:
        # Дальше не смотрим: у комнаты нет ни марки, ни коробки, и дописывать
        # «чужая марка» к «чужая категория» значит мерить один дефект четырежды.
        return "чужая категория"
    # Оффер аренды покупателю — чужая сторона сделки, жёсткий факт того же рода,
    # что категория (spec-v2 2.7). Судит правдой `rental`, а не текстом: мерка
    # читает Lot, а не парсит объявление. Арендатору (`intent=RENT`) прокат нужен,
    # поэтому проверка только у покупателя.
    if passport.intent is Intent.BUY and lot.rental:
        return "оффер аренды покупателю"
    attributes = passport.attributes
    checks: tuple[tuple[bool, str], ...] = (
        (_mismatch(attributes.get("brand"), lot.brand), "чужая марка"),
        (_mismatch(attributes.get("model"), lot.model), "чужая модель"),
        (_mismatch(attributes.get("transmission"), lot.transmission), "чужая коробка"),
        (_wrong_rooms(lot, attributes.get("rooms")), "чужие комнаты"),
        (_over_budget(lot, passport.budget), "дороже бюджета"),
        (_wrong_engine(lot, attributes.get("engine_cc")), "не тот объём"),
    )
    return ", ".join(reason for failed, reason in checks if failed)


def _mismatch(wanted: object, actual: str) -> bool:
    """Клиент назвал значение, а у лота оно другое. Молчание расхождением не считается."""
    return bool(wanted) and str(wanted).casefold() != actual.casefold()


def _over_budget(lot: Lot, budget: Budget) -> bool:
    ceiling = ceiling_vnd(budget)
    if ceiling is None or lot.item.price_vnd is None:
        return False
    return lot.item.price_vnd > ceiling


def ceiling_vnd(budget: Budget) -> float | None:
    """Потолок бюджета в донгах. Своя арифметика, а не ранжировщика, — намеренно."""
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return budget.max
    if budget.currency is Currency.USD:
        return budget.max * USD_VND
    return None


def _wrong_engine(lot: Lot, wanted: object) -> bool:
    if wanted is None or lot.engine_cc is None:
        return False
    asked = float(str(wanted))
    return abs(lot.engine_cc - asked) > asked * ENGINE_CC_TOLERANCE


def _wrong_rooms(lot: Lot, wanted: object) -> bool:
    """Число комнат лота не то, что просил клиент. Молчание расхождением не считается.

    Жёсткий факт, как модель и объём: «2 спальни» против студии — разное жильё
    (passport.md, spec-v2 2.7). Точное совпадение, а не «хотя бы»: 3 комнаты на
    запрос двух — другой сегмент. Лот, число комнат не назвавший (`rooms=None`),
    не противоречит — половина объявлений его не пишет.
    """
    if wanted is None or lot.rooms is None:
        return False
    return int(str(wanted)) != lot.rooms
