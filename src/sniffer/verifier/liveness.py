"""Живость лота — spec-v2, раздел 3.3.

Главная боль ручного поиска: объявление висит с мая, вещь продана в июне.
Объявления не снимают почти никогда, поэтому дата публикации — не украшение
карточки, а единственный честный сигнал, который есть у нас до первого
обращения к продавцу.

Здесь только вердикт: `stale`, `fresh` или «неизвестно». Формулировка для
клиента живёт в слое показа — проверяльщик решает, а не разговаривает.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

# spec-v2, 3.3. Порог грубый и таким и задуман: точной даты продажи нет ни у
# кого, а две недели — тот возраст, после которого байк в Нячанге чаще продан,
# чем нет.
STALE_AFTER_DAYS = 14


class Liveness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    # Карточка с unknown в выдачу попадает, но с пометкой (spec-v2, 3.4).
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class LivenessVerdict:
    status: Liveness
    age_days: int | None

    @property
    def is_stale(self) -> bool:
        return self.status is Liveness.STALE


def assess(posted_at: datetime | None, *, now: datetime | None = None) -> LivenessVerdict:
    """Возраст публикации → вердикт. Без сети и без модели: это просто дата."""
    if posted_at is None:
        return LivenessVerdict(Liveness.UNKNOWN, None)

    moment = now or datetime.now(UTC)
    age = as_utc(moment) - as_utc(posted_at)
    # Отрицательный возраст — кривая метка у источника, а не машина времени.
    # Считаем такую публикацию свежей: занижать возраст безопаснее, чем
    # молча повысить доверие к старому лоту.
    age_days = max(age.days, 0)
    status = Liveness.STALE if age_days > STALE_AFTER_DAYS else Liveness.FRESH
    return LivenessVerdict(status, age_days)


def as_utc(moment: datetime) -> datetime:
    """Наивную метку считаем UTC.

    Источники отдают либо UTC, либо эпоху; часовой пояс клиента к возрасту
    объявления отношения не имеет, а `TypeError` на вычитании наивного из
    осведомлённого уронил бы выдачу целиком.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
