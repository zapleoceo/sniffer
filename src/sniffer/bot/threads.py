"""Открытие ветки: одно место, где у человека появляется новый запрос.

Зачем отдельным модулем, а не строчкой в `conversation`. Ветка рождается двумя
путями — по `/new` и по сообщению, которое ни одна эвристика не признала
уточнением, — и оба обязаны вести себя одинаково: занять место в списке,
назвать вытесненную ветку и не потерять её. Положи это в `conversation` дважды,
и две копии разойдутся; положи один раз внутри хода диалога — и это уже третья
ответственность файла, который и так ведёт ход и формулирует ответы.

Про `send` этот модуль не знает намеренно. Он возвращает текст, а отправляет его
`conversation`: иначе `bot.threads` импортировал бы `Reply` и `Send` из
`conversation`, который импортирует `bot.threads`, — круг.
"""

from __future__ import annotations

from dataclasses import dataclass

from sniffer.bot.naming import category_noun
from sniffer.bot.store import Dialogue, DialogueStore
from sniffer.domain.passport import Passport
from sniffer.domain.threads import MAX_LIVE_THREADS, pushed_out
from sniffer.search.vocabulary import city_name

# Сколько символов заголовка влезает в кнопку Telegram, не уезжая в многоточие
# на телефоне.
TITLE_LIMIT = 38

ASK_WHAT = (
    "Новый поиск. Напишите словами, что ищете, — прежние поиски никуда не делись, они в /requests."
)
# Предел подставляется, а не вписан словом: «это шестой поиск» рядом с
# именованной константой — это лишняя копия числа, и первая же правка предела
# сделала бы её ложью, не тронув ни одного теста.
PUSHED_OUT = (
    "Поисков в работе уже {limit}, поэтому «{title}» ушёл из списка /requests. "
    "Он не удалён: мониторинг работает, а кнопки под его выдачей по-прежнему "
    "возвращают к нему."
)


@dataclass(frozen=True, slots=True)
class Opened:
    """Ветка открыта. `notice` — то, что человеку надо сказать про вытеснение."""

    dialogue: Dialogue
    notice: str | None = None


async def open_thread(store: DialogueStore, dialogue: Dialogue, passport: Passport) -> Opened:
    """Новая ветка под эту просьбу.

    Место проверяется ДО создания: после `start` вытесненная ветка уже не видна
    в списке, и назвать её человеку было бы нечем.
    """
    crowded = pushed_out(await store.live_threads(dialogue))
    opened = await store.start(dialogue, passport)
    notice = (
        None
        if crowded is None
        else PUSHED_OUT.format(limit=MAX_LIVE_THREADS, title=title(crowded.passport))
    )
    return Opened(dialogue=opened, notice=notice)


def title(passport: Passport, *, limit: int = TITLE_LIMIT) -> str:
    """Короткое узнаваемое имя ветки без отдельного шага «назовите запрос».

    Собирается из предмета, марки с моделью и города — «Скутер Honda, Нячанг», —
    а не из формулировки клиента. Формулировка меняется с каждой правкой
    («до 500», «не скутер, а мотоцикл»), и подпись кнопки прыгала бы вместе с
    ней: человек искал бы в списке ту строку, которую запомнил, а её там уже нет.

    Марка и модель в заголовке не для подробности: два поиска байков в одном
    городе — обычное дело, и «Скутер, Нячанг» дважды не различить вовсе.
    """
    subject = " ".join(part for part in (category_noun(passport), _model(passport)) if part)
    if subject:
        name = ", ".join(part for part in (subject, city_name(passport.city, "ru")) if part)
    else:
        # Предмет ещё не известен, и один город ветку не назовёт: «Нячанг» не
        # говорит, что искали. Тогда берём сказанное — оно хотя бы про запрос.
        name = passport.raw_query.strip() or "запрос"
    # С заглавной: это имя, а не продолжение фразы. `capitalize()` не годится —
    # он опускает остальные буквы, и «Скутер Honda» стал бы «Скутер honda».
    name = name[0].upper() + name[1:]
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _model(passport: Passport) -> str:
    """Марка и модель так, как их назвал человек: «Honda Lead», «Yamaha»."""
    said = [
        str(passport.attributes[key]) for key in ("brand", "model") if passport.attributes.get(key)
    ]
    return " ".join(said).title()
