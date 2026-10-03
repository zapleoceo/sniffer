"""Поиски человека: как открываются, как называются и что про них говорит бот.

Зачем отдельным модулем, а не строчкой в `conversation`. Поиск (ветка) рождается
двумя путями — по `/new` и по сообщению, которое ни одна эвристика не признала
уточнением, — и оба обязаны вести себя одинаково: занять место в списке,
назвать вытесненный поиск и не потерять его. Положи это в `conversation` дважды,
и две копии разойдутся; положи один раз внутри хода диалога — и это уже третья
ответственность файла, который и так ведёт ход и формулирует ответы.

Про `send` этот модуль не знает намеренно. Он возвращает текст, а отправляет его
`conversation`: иначе `bot.threads` импортировал бы `Reply` и `Send` из
`conversation`, который импортирует `bot.threads`, — круг.

Название поиска попадает в два разных места, и обращаться с ним там надо по-разному.
В подпись кнопки оно идёт как есть: кнопка — обычный текст, и экранирование
показало бы человеку `&lt;`. В сообщение с `parse_mode=HTML` оно идёт ТОЛЬКО через
`bold_title` / `pushed_out_notice` / `edit_prompt`: сырое «<» в названии (оно
берётся из слов клиента, когда предмет неизвестен) Telegram отвергает целиком, и
человек не получает ответа вовсе.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from sniffer.bot.naming import budget_phrase, category_noun, intent_label
from sniffer.bot.store import Dialogue, DialogueStore
from sniffer.domain.passport import Passport
from sniffer.domain.records import QueryOverview
from sniffer.domain.threads import MAX_LIVE_THREADS, pushed_out
from sniffer.search.vocabulary import city_name

# Сколько символов названия влезает в кнопку Telegram, не уезжая в многоточие
# на телефоне.
TITLE_LIMIT = 38
# Короче название не режем, даже уступая место бюджету: от «Мотоб… · до 400 USD»
# пользы меньше, чем от длинной подписи, которую клиент Telegram обрежет сам.
_MIN_NAME = 14

ASK_WHAT = (
    "Новый поиск. Напишите или наговорите, что ищете. Прежние поиски сохранены — они в /requests."
)
# Предел подставляется, а не вписан словом: «не больше пяти» рядом с именованной
# константой — это лишняя копия числа, и первая же правка предела сделала бы её
# ложью, не тронув ни одного теста. Про мониторинг говорим только то, что есть:
# прежний текст всегда утверждал «мониторинг работает», а подписок у вытесненных
# поисков по факту не было, и человек ждал уведомлений, которых нет.
PUSHED_OUT = (
    "В списке /requests не больше {limit} поисков, поэтому «{title}» из него убран. "
    "Сам поиск сохранён{monitoring}."
)
_MONITORING = {"active": ", мониторинг продолжает работать", "paused": ", мониторинг на паузе"}

NO_SEARCHES = "Поисков пока нет. Напишите, что хотите найти."
NOT_FOUND = "Этот поиск не найден. Откройте список заново."
MONITORING_ENDED = (
    "У этого поиска нет слота слежения: подписка закончилась или слот занят другим поиском. "
    "Поиск и настройки сохранены — продлите подписку или перенесите слот."
)
LIST_HEADER = "Ваши поиски. ✓ — текущий: следующие сообщения уточняют его."
# Пока взведён `/new`, «✓» ничего не обещает: следующее сообщение уйдёт в НОВЫЙ
# поиск, а не в отмеченный.
LIST_HEADER_NEW = "Следующее сообщение начнёт новый поиск."
LIST_LEGEND = "🟢 мониторинг работает · ⏸ на паузе · ⌛ нет слота · ▫️ без мониторинга"
EDIT_PROMPT = (
    "Изменяем: {title}\n\n"
    "Напишите, что изменить, например «до 500», или новую формулировку целиком."
)
_STATES = {
    "active": "мониторинг работает",
    "paused": "мониторинг на паузе",
    "expired": "нет слота слежения: поиск сохранён, слот можно продлить или перенести",
    "off": "мониторинг не подключён",
}


@dataclass(frozen=True, slots=True)
class Opened:
    """Поиск открыт. `notice` — то, что человеку надо сказать про вытеснение."""

    dialogue: Dialogue
    notice: str | None = None


async def open_thread(
    store: DialogueStore, dialogue: Dialogue, passport: Passport
) -> Opened | None:
    """Новый поиск под эту просьбу. `None` — `/new` уже потрачен другим сообщением.

    Место проверяется ДО создания: после `start` вытесненный поиск уже не виден
    в списке, и назвать его человеку было бы нечем.

    Взведённое `/new` тратится вместе с созданием, одним действием хранилища
    (`start_requested`): второе сообщение, прочитавшее флаг до того, как первое
    успело его потратить, получает отказ и не открывает ещё один поиск.
    """
    live = await store.live_threads(dialogue)
    crowded = pushed_out(live)
    if dialogue.starting_new:
        opened = await store.start_requested(dialogue, passport)
        if opened is None:
            return None
    else:
        opened = await store.start(dialogue, passport)
    notice = None if crowded is None else pushed_out_notice(crowded, live)
    return Opened(dialogue=opened, notice=notice)


def pushed_out_notice(crowded: QueryOverview, live: Sequence[QueryOverview]) -> str:
    """Что сказать про вытесненный поиск — по его настоящему мониторингу.

    Раньше текст всегда утверждал «мониторинг работает», а подписок у вытесненных
    поисков по факту не было вовсе: человек ждал бы уведомлений, которых нет. Фраза
    про мониторинг появляется, только если подписка есть, и называет её состояние.

    Название берётся из подписей всего списка (`labels`), а не само по себе: два
    поиска с одинаковым названием различимы бюджетом, и «Мотобайк, Нячанг» без
    него назвал бы человеку то, что в списке стоит дважды.
    """
    names = labels([item.passport for item in live])
    named = next(
        (name for item, name in zip(live, names, strict=True) if item.root == crowded.root),
        title(crowded.passport),
    )
    return PUSHED_OUT.format(
        limit=MAX_LIVE_THREADS,
        title=escape(named, quote=False),
        monitoring=_MONITORING.get(crowded.monitoring, ""),
    )


def list_text(*, starting_new: bool) -> str:
    return f"{LIST_HEADER_NEW if starting_new else LIST_HEADER}\n\n{LIST_LEGEND}"


def card_text(item: QueryOverview) -> str:
    return f"{bold_title(item.passport)}\n{_STATES[item.monitoring]}"


def edit_prompt(passport: Passport) -> str:
    return EDIT_PROMPT.format(title=bold_title(passport))


def bold_title(passport: Passport) -> str:
    """Название жирным для сообщения с HTML: единственное место, где его экранируют."""
    return f"<b>{escape(title(passport), quote=False)}</b>"


def title(passport: Passport, *, limit: int = TITLE_LIMIT) -> str:
    """Короткое узнаваемое имя поиска без отдельного шага «назовите запрос».

    Собирается из предмета, марки с моделью и города — «Скутер Honda, Нячанг», —
    а не из формулировки клиента. Формулировка меняется с каждой правкой
    («до 500», «не скутер, а мотоцикл»), и подпись кнопки прыгала бы вместе с
    ней: человек искал бы в списке ту строку, которую запомнил, а её там уже нет.

    Марка и модель в заголовке не для подробности: два поиска байков в одном
    городе — обычное дело, и «Скутер, Нячанг» дважды не различить вовсе. По той же
    причине в нём есть сторона сделки, когда она нарушает умолчание категории:
    «Аренда: мотобайк, Нячанг» рядом с «Мотобайк, Нячанг».
    """
    return _fit(_name(passport), limit)


def labels(passports: Sequence[Passport], *, limit: int = TITLE_LIMIT) -> list[str]:
    """Подписи списка: у двух разных поисков одной подписи не бывает.

    Название стабильно нарочно, а значит два поиска «Мотобайк, Нячанг» с разным
    бюджетом получили бы две одинаковые кнопки. Различитель добавляется ТОЛЬКО
    тогда, когда подписи совпали: бюджет в каждом названии заставил бы подпись
    прыгать на каждой правке «до 500».
    """
    names = [title(passport, limit=limit) for passport in passports]
    twins = Counter(names)
    return [
        name if twins[name] == 1 else _tell_apart(passport, limit)
        for passport, name in zip(passports, names, strict=True)
    ]


def _tell_apart(passport: Passport, limit: int) -> str:
    extra = budget_phrase(passport)
    if not extra:
        return title(passport, limit=limit)
    tail = f" · {extra}"
    return _fit(_name(passport), max(limit - len(tail), _MIN_NAME)) + tail


def _name(passport: Passport) -> str:
    subject = " ".join(part for part in (category_noun(passport), _model(passport)) if part)
    if subject:
        place = city_name(passport.city, "ru")
        name = ", ".join(part for part in (subject, place) if part)
        side = intent_label(passport)
        name = _clean(f"{side}: {name}" if side else name)
    else:
        # Предмет ещё не известен, и один город ветку не назовёт: «Нячанг» не
        # говорит, что искали. Тогда берём сказанное — оно хотя бы про запрос.
        # Первая строка: служебный хвост «Последнее уточнение…» дописывается
        # после перевода строки и в название попадать не должен.
        name = _clean(_first_line(passport.raw_query)) or "запрос"
    # С заглавной: это имя, а не продолжение фразы. `capitalize()` не годится —
    # он опускает остальные буквы, и «Скутер Honda» стал бы «Скутер honda».
    return name[0].upper() + name[1:]


def _model(passport: Passport) -> str:
    """Марка и модель так, как их назвал человек: «Honda Lead», «Yamaha NVX»."""
    said = [
        str(passport.attributes[key]) for key in ("brand", "model") if passport.attributes.get(key)
    ]
    return " ".join(_capitalised(word) for word in " ".join(said).split())


def _capitalised(word: str) -> str:
    """Заглавная только у чисто буквенных слов: «honda» → «Honda», но «sh150i» и «PCX» целы.

    `str.title()` калечил модели: «Sh150I», «Pcx», «Mt-15».
    """
    return word[:1].upper() + word[1:] if word.isalpha() else word


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _clean(text: str) -> str:
    """Пробельные символы — в один пробел, управляющие и невидимые — долой.

    Название идёт в кнопку и в сообщение: перевод строки ломает ряд кнопок, а
    нулевой пробел или смена направления письма (RLO) подделывают подпись.
    """
    kept = "".join(
        " " if char.isspace() else char
        for char in text
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    return " ".join(kept.split())


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
