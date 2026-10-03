"""Что разовая команда обязана знать об исключениях одинаково на каждом шаге.

Команда с закрытым набором кодов возврата (`enrich`) отвечает на любую
поломку документированным кодом, а не трейсбеком. Полнота набора держится
построением: каждый шаг, который делает работу, стоит внутри блока, чей
последний `except` — `BaseException`, то есть корень иерархии, а не список
ожидаемых классов (CLAUDE.md, «Как закрывают набор кодов возврата»). Здесь —
то, что такие блоки обязаны решать одинаково, чтобы шаг не выбирал сам:

* что перебрасывается, а не отвечается (`reraise_if_not_ours`);
* что считается прерыванием, а не сбоем (`is_interrupt`);
* как назвать сбой, ничего не выдав (`class_chain`).
"""

from __future__ import annotations

import asyncio

# Цепочка причин короче, чем кажется: сбой базы — это обёртка SQLAlchemy,
# адаптер и исключение драйвера. Больше четырёх имён не добавляют понимания.
MAX_CHAIN = 4


def reraise_if_not_ours(err: BaseException) -> None:
    """`SystemExit` и `GeneratorExit` — не поломка, а требование остановиться.

    Единственный карв-аут в охране, и он именно ПЕРЕбрасывает, а не глотает:
    просьбу выйти с чужим кодом нельзя переписать своим, а проглоченный
    `GeneratorExit` ломает контракт интерпретатора. Живёт одним местом, чтобы
    шаг не выбирал сам: шагу остаётся только корень иерархии.
    """
    if isinstance(err, SystemExit | GeneratorExit):
        raise err


def is_interrupt(err: BaseException) -> bool:
    """Прервали ли команду: Ctrl+C, снятая задача — или ГРУППА из них.

    Группа проверяется отдельно: `BaseExceptionGroup` не наследует ни
    `KeyboardInterrupt`, ни `Exception`, а `asyncio.TaskGroup` и
    `asyncio.timeout` заворачивают снятую задачу именно в неё. Смешанная группа
    прерыванием НЕ считается: если рядом со снятой задачей приехал настоящий
    сбой, ответ обязан быть про сбой.
    """
    if isinstance(err, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(err, BaseExceptionGroup):
        return all(is_interrupt(sub) for sub in err.exceptions)
    return False


def _name(err: BaseException) -> str:
    name = type(err).__name__
    if isinstance(err, BaseExceptionGroup):
        return f"{name}[{', '.join(type(sub).__name__ for sub in err.exceptions)}]"
    return name


def _cause(err: BaseException) -> BaseException | None:
    # SQLAlchemy кладёт исключение драйвера и в `__cause__`, и в `orig`;
    # `orig` — запасной путь для обёрток, которые причину не сцепили.
    if err.__cause__ is not None:
        return err.__cause__
    orig = getattr(err, "orig", None)
    return orig if isinstance(orig, BaseException) else None


def class_chain(err: BaseException) -> str:
    """Имена классов сбоя и его причин, от следствия к причине — БЕЗ текста.

    Текст исключения базы содержит параметры SQL, а параметры прохода по
    карточкам — заголовки и атрибуты объявлений с телефонами и @username.
    Лог и вывод команды уходят в `docker logs` и в историю терминала, поэтому
    печатаем только то, что про сбой, а не про данные: «DBAPIError <-
    NumericValueOutOfRangeError» говорит достаточно, чтобы найти причину.
    `str(err)` не вызывается вовсе: у чужого типа он может и сам упасть.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen and len(names) < MAX_CHAIN:
        seen.add(id(current))
        names.append(_name(current))
        current = _cause(current)
    return " <- ".join(names)
