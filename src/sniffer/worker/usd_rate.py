"""Курс доллара для монитора: есть он или его нет — третьего не бывает.

Долларовый бюджет подписки становится потолком в донгах только с курсом. Без него монитор
не имеет права «слать без фильтра бюджета»: подписка шлёт сама, без спроса, и дорогое
объявление ушло бы клиенту как «идеально в бюджете». Поэтому подписка с долларовым
бюджетом при недоступном курсе ЖДЁТ — её курсор стоит, ничего не теряется, а когда курс
вернётся, проход возьмёт всё с того же места (решение владельца 03.10.2026: ждать).

Сам источник курса (`search.currency.usd_vnd_rate`) уже держит удачный ответ шесть часов, но
неудачу не помнит: без этого класса недоступный сервис опрашивали бы на каждом проходе
воркера, то есть раз в несколько секунд, и каждый раз ждали бы его таймаут.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

log = structlog.get_logger(__name__)

# Откуда брать курс. Внедряется, а не импортируется: матчер собирается в тесте без сети, а
# забытая проводка видна как «источника нет», а не как тихий поход в интернет.
RateSource = Callable[[], Awaitable[float | None]]

# Через сколько снова спросить источник, который не ответил. Минуты, а не секунды: курс
# меняется на доли процента в сутки, и срочности в повторе нет.
RETRY_AFTER = timedelta(seconds=60)


class UsdRate:
    """Курс USD→VND для прохода. Один вопрос — один ответ, без исключений наружу."""

    def __init__(
        self, source: RateSource | None = None, *, retry_after: timedelta = RETRY_AFTER
    ) -> None:
        self._source = source
        self._retry_after = retry_after
        self._retry_at: datetime | None = None
        self._down = False

    async def get(self, now: datetime) -> float | None:
        """Курс или `None`, если его нет. Никогда не бросает.

        Широкий `except` здесь намеренно: источник — чужой сервис, и перечислять его отказы
        значит однажды уронить весь проход матчера на неназванном. Отказ не глотается: он
        в логе с типом ошибки и означает ровно то же, что «курса нет».
        """
        if self._source is None:
            return None
        if self._retry_at is not None and now < self._retry_at:
            return None
        try:
            rate = await self._source()
            if rate is not None and not (math.isfinite(rate) and rate > 0):
                # Нуль, минус и NaN — не курс. NaN хуже всех: в Postgres он больше любого
                # числа, и потолок «до NaN» пропустил бы всё, то есть вернул бы тот же дефект.
                raise ValueError(f"не курс: {rate!r}")
        except Exception as exc:
            log.warning("fx.usd_rate_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            rate = None
        if rate is None:
            self._retry_at = now + self._retry_after
            if not self._down:
                # Один раз на простой, а не на каждую попытку: журнал отказа раз в минуту
                # для тех, кто ждёт курс часами, — шум.
                log.warning("fx.usd_rate_unavailable", retry_in_s=self._retry_after.total_seconds())
            self._down = True
            return None
        if self._down:
            log.info("fx.usd_rate_restored", rate=rate)
        self._retry_at = None
        self._down = False
        return rate
