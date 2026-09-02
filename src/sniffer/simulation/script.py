"""Из чего состоит сценарий: словарь шагов и форма ожиданий.

Отдельно от самой таблицы (`scenarios.py`), потому что это две разные вещи и
меняются они по разным поводам. Здесь — устройство: чем клиент вправе ответить
и что о сценарии можно утверждать. Там — данные: два десятка живых формулировок
рынка. Правка формы задевает все сценарии сразу, правка сценария — только его,
и держать их в одном файле значит каждый раз перечитывать чужое.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sniffer.domain.dialogue import Feedback


@dataclass(frozen=True, slots=True)
class Says:
    """Клиент пишет текстом."""

    text: str


@dataclass(frozen=True, slots=True)
class Taps:
    """Клиент нажимает кнопку под висящим вопросом.

    Код вопроса не указывается: клиент нажимает то, что бот только что прислал,
    и харнес берёт код оттуда же. Указывай мы код руками — сценарий проверял бы
    нашу память о клавиатуре, а не поведение бота.
    """

    value: str


@dataclass(frozen=True, slots=True)
class Reacts:
    """Клиент нажимает кнопку под карточками: «дорого», «не то», «нужен автомат»."""

    feedback: Feedback


Step = Says | Taps | Reacts


@dataclass(frozen=True, slots=True)
class Wish:
    """Ожидание, которое пока не выполняется, и причина этого."""

    fields: Mapping[str, object]
    why: str


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    # Ноль, когда категория известна из фразы; один — когда её неоткуда взять.
    # Больше одного вопроса до выдачи — это и есть «бот тупит» (passport.md).
    max_questions_before_results: int = 0
    # `True` — карточки обязаны прийти. `False` — обязаны НЕ прийти: верный
    # ответ другой (город, где мы не ищем). `None` — всё равно, и это не
    # лень: сценарий обрывается на вопросе, и если завтра бот сумеет ответить
    # выдачей вместо вопроса, это улучшение, а не дефект. Записать здесь
    # `False` значило бы запретить боту поумнеть.
    expect_results: bool | None = True
    expect: Mapping[str, object] = field(default_factory=dict)
    # Поля, которые обязаны остаться ПУСТЫМИ: «200 кубиков» не бюджет.
    forbid: tuple[str, ...] = ()
    # Подстрока, которая обязана прозвучать. Нужна там, где правильный ответ —
    # не карточки, а отказ по делу.
    expect_text: str = ""
    wish: Wish | None = None
