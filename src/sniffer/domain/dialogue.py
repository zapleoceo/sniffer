"""Уточняющий диалог: какой вопрос задать и что делать с ответом.

Правило продукта (passport.md): спрашиваем только поля, которые реально сужают
выдачу, и не больше трёх вопросов за диалог. Дальше показываем выдачу и
уточняем паспорт обратной связью на карточках — человеку проще сказать
«дорого», глядя на пять карточек, чем назвать бюджет в пустоту.

Модуль чистый: ни ввода-вывода, ни aiogram, ни знания о Telegram. Бот только
показывает выбранный здесь вопрос и приносит обратно ответ.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import ceil

from sniffer.domain.passport import (
    FIELD_INFORMATIVENESS,
    MAX_CLARIFYING_QUESTIONS,
    MAX_FEEDBACK_QUESTIONS,
    Budget,
    Category,
    Currency,
    Passport,
    PassportStatus,
    has_value,
    next_questions,
)
from sniffer.domain.records import PassportEvent

# Значение «не важно». Отдельное от пустого ответа: пустое поле означает «ещё
# не спрашивали», а SKIP — «спросили, клиенту всё равно».
SKIP = "skip"
SKIP_LABEL = "не важно, показать что есть"

# Виды событий паспорта (passport.md, «Версионирование»). `question_asked` —
# не правка паспорта, а след диалога: из него собирается счётчик заданных
# вопросов, который обязан пережить перезапуск процесса.
EVENT_USER_MESSAGE = "user_message"
EVENT_FEEDBACK = "feedback"
EVENT_AGENT_INFER = "agent_infer"
EVENT_MANUAL_EDIT = "manual_edit"
EVENT_QUESTION_ASKED = "question_asked"

# Насколько режем бюджет по кнопке «дорого». Не в ноль и не в половину:
# клиент отбраковал показанное, а не отказался от покупки.
PRICEY_FACTOR = 0.7

# Готовые суммы бюджета. Валюта лежит в той же строке, что и подпись, и
# значение кнопки строится из обеих — иначе они расходятся молча: подпись
# говорила «до 300 $», а уезжала валюта ПАСПОРТА, и клиенту, сказавшему «за
# донги», кнопка «до 300 $» отправляла 300 донгов — это 1.2 цента.
_BUDGET_CHOICES: tuple[tuple[int, Currency], ...] = (
    (300, Currency.USD),
    (500, Currency.USD),
    (800, Currency.USD),
)
_CURRENCY_SIGN: dict[Currency, str] = {Currency.USD: "$", Currency.VND: "₫"}

# Когда два слова — одно и то же слово в разных падежах (`_same_stem`).
# Падежное окончание просьбу не меняет: «нячанге» и «нячанг» — один город,
# «квартиру» и «квартира» — одна категория.
#
# Раньше здесь стояло фиксированное число первых букв (четыре), и оно ломалось
# в обе стороны сразу, потому что длина слова разная: «дом» и «дома» четырёх
# букв не набирали и считались РАЗНЫМИ словами, а «Куангнгай» и «Куангнам» —
# два разных города, которых нет ни в одном справочнике, — совпадали по «куан»
# и читались как ОДИН. Вьетнам плотен такими кустами: Куангнгай, Куангнам,
# Куангнинь, Куангбинь, Куангчи; Биньдинь, Биньтхуан; Тхайнгуен, Тхайбинь.
#
# Поэтому мера относительная: общая часть должна покрывать БОЛЬШУЮ ДОЛЮ
# короткого слова, а не заданное число букв. «дом»/«дома» — 3 из 3, один
# корень; «Куангнгае»/«Куангнаме» — 6 из 9, разные; «автомат»/«автобус» — 4 из
# 7, разные. Нижний порог в три буквы остаётся: по двум буквам сошлось бы что
# угодно.
_STEM_SHARE = 0.75
_STEM_MIN = 3

# Слово — последовательность буквенно-цифровых символов. Знаки и регистр
# формулировку не меняют: «Ищу скутер в Нячанге!» — тот же запрос.
_WORD_RE = re.compile(r"\w+")

# Слова, которые не несут содержания просьбы: предлоги, союзы, частицы,
# вежливость и глаголы самого обращения. Нужны потому, что «новое слово» —
# критерий повтора, а дописанное «с» или «пожалуйста» новых сведений не даёт:
# без этого списка лимит трёх вопросов обходился приписыванием предлога
# (замерено: 9 вопросов вместо 3).
#
# Это таблица о языке, а не список исключений: критерий членства — «слово
# служебное, запрос без него тот же». Пропущенная частица не ломает ничего
# опасного — она делает формулировку новым запросом, то есть возвращает
# прежнее поведение.
_FILLER_WORDS: frozenset[str] = frozenset(
    {
        # предлоги и союзы
        "в",
        "во",
        "на",
        "за",
        "до",
        "от",
        "с",
        "со",
        "и",
        "а",
        "но",
        "или",
        "по",
        "у",
        "о",
        "об",
        "для",
        "при",
        "же",
        "ли",
        "бы",
        # Частицы и вежливость. «не» здесь НЕТ намеренно: отрицание
        # переворачивает просьбу («скутер автомат» → «скутер не автомат»), и
        # посчитать его украшением значило бы принять смену запроса за повтор.
        "ну",
        "вот",
        "тут",
        "там",
        "пожалуйста",
        "плиз",
        "спасибо",
        "срочно",
        "очень",
        "можно",
        "надо",
        "нужно",
        # глаголы обращения: они про клиента, а не про предмет
        "ищу",
        "искать",
        "нужен",
        "нужна",
        "нужны",
        "хочу",
        "хотел",
        "смотрю",
        "подскажи",
        "подскажите",
        "помоги",
        "помогите",
        "интересует",
        # английские служебные — рынок трёхъязычный
        "in",
        "at",
        "for",
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "please",
        "need",
        "want",
        "looking",
        "i",
        "am",
        "is",
    }
)

AnswerValue = str | float | Budget


class Feedback(StrEnum):
    PRICEY = "pricey"
    WRONG = "wrong"
    AUTOMATIC = "automatic"


@dataclass(frozen=True, slots=True)
class Option:
    """Кнопка ответа. `value` уезжает в callback_data, поэтому короткий."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Question:
    """Вопрос про одно поле паспорта.

    `code` — короткий ключ поля для callback_data: в неё влезает 64 байта, а
    `attributes.transmission` съело бы треть бюджета кириллицей.
    """

    field: str
    code: str
    text: str
    options: tuple[Option, ...] = ()
    skippable: bool = True

    @property
    def buttons(self) -> tuple[Option, ...]:
        """Варианты ответа и выход для полей, которые можно не ограничивать."""
        if not self.skippable:
            return self.options
        return (*self.options, Option(SKIP_LABEL, SKIP))


# Спрашиваем только про то, что умеем спросить кнопками. Поле без вопроса
# (например `districts` — справочника районов пока нет) просто пропускается:
# лучше не спросить, чем спросить так, что ответ нечем разобрать.
QUESTIONS: tuple[Question, ...] = (
    Question(
        field="category",
        code="cat",
        text="Что ищем?",
        options=(
            Option("скутер", "scooter"),
            Option("мотобайк", "motorbike"),
            Option("квартиру", "apartment"),
            Option("комнату", "room"),
            Option("дом", "house"),
        ),
        skippable=False,
    ),
    Question(
        field="city",
        code="city",
        text="В каком городе ищем?",
        options=(Option("Нячанг", "nha_trang"), Option("Дананг", "da_nang")),
        skippable=False,
    ),
    Question(
        field="budget.max",
        code="budget",
        text="Какой бюджет? Можно написать словами — «до 400» или «до 10 млн».",
        options=tuple(
            Option(f"до {amount} {_CURRENCY_SIGN[currency]}", f"{amount} {currency.value}")
            for amount, currency in _BUDGET_CHOICES
        ),
    ),
    Question(
        field="attributes.transmission",
        code="trans",
        text="Автомат или механика?",
        options=(Option("автомат", "automatic"), Option("механика", "manual")),
    ),
    Question(
        field="attributes.condition",
        code="cond",
        text="Состояние?",
        options=(
            Option("новый", "new"),
            Option("хороший", "good"),
            Option("любой, лишь бы ездил", "worn"),
        ),
    ),
    Question(
        field="attributes.brand",
        code="brand",
        text="Есть марка на примете?",
        options=(Option("Honda", "honda"), Option("Yamaha", "yamaha")),
    ),
    Question(
        field="attributes.rooms",
        code="rooms",
        text="Сколько комнат?",
        options=(Option("студия", "1"), Option("две", "2"), Option("три и больше", "3")),
    ),
)

_BY_FIELD: dict[str, Question] = {question.field: question for question in QUESTIONS}
_BY_CODE: dict[str, Question] = {question.code: question for question in QUESTIONS}


def question_for(field: str) -> Question | None:
    return _BY_FIELD.get(field)


def question_by_code(code: str) -> Question | None:
    return _BY_CODE.get(code)


def blocking_question(passport: Passport, asked: Sequence[str]) -> Question | None:
    """Минимальная воронка до поиска: предмет, город и ценовой потолок.

    Уже названное не переспрашиваем. Бюджет можно явно пропустить кнопкой,
    после чего поле остаётся пустым, но событие в `asked` разрешает поиск.
    Категорию и город пропускать нельзя: широкий поиск по всему рынку или
    молчаливая подстановка города выдают случайные варианты.
    """
    for field in ("category", "city", "budget.max"):
        if not has_value(passport, field) and (field != "budget.max" or field not in asked):
            return question_for(field)
    return None


def next_question(
    passport: Passport,
    asked: Sequence[str],
    *,
    limit: int = MAX_CLARIFYING_QUESTIONS,
) -> Question | None:
    """Следующее уточнение по обратной связи — или ничего, если спрашивать нечего.

    Зовётся только из `feedback_question`: клиент нажал «не то», и вопрос теперь
    уместен. До первой выдачи он не звучит — там работает `blocking_question`.

    `limit` считает вопросы всего диалога, а не оставшиеся: цепочка версий
    паспорта помнит, о чём уже спрашивали, и после перезапуска бот не начинает
    допрос заново.
    """
    if len(asked) >= limit:
        return None
    for field in _ranked(passport):
        if field in asked:
            continue
        question = question_for(field)
        if question is not None:
            return question
    return None


def _ranked(passport: Passport) -> list[str]:
    """Незаполненные поля по убыванию информативности — весь список, не топ-3.

    Отсев уже спрошенных и неспрашиваемых полей идёт после ранжирования:
    обрезать список до лимита раньше отсева значило бы промолчать там, где
    следующее поле спросить было можно.
    """
    if passport.category is None:
        # Без категории спрашивать больше нечего: набор полей зависит от неё.
        return next_questions(passport, limit=1)
    weights = FIELD_INFORMATIVENESS.get(passport.category, {})
    return next_questions(passport, limit=len(weights) or 1)


def restates(current: Passport, fresh: Passport) -> bool:
    """Клиент повторил ту же просьбу, а не сформулировал новую.

    Свободный текст — это либо ответ на вопрос, либо новый запрос, и различить
    их обязан кто-то: приняв повтор за новый запрос, бот обнуляет собранные
    ответы и счётчик вопросов, то есть лимит в три вопроса обходится копипастом
    собственной фразы.

    Критерий двойной, и обе половины нужны:

    * **Разбор** — всё, что новая формулировка сумела прочитать: намерение,
      категория, город, бюджет, атрибуты. Это и есть запрос; сказал иначе
      хоть про одно — тема другая, и новая цепочка версий здесь единственно
      верна. «Ищу скутер в Дананге» после «ищу скутер в Нячанге» отличается
      одним словом из четырёх, но это другой запрос, и решает это город, а не
      похожесть текста.
    * **Слова** — приносит ли формулировка хоть одно новое СОДЕРЖАТЕЛЬНОЕ
      слово. Разбор видит не всё: «ищу скутер, только подешевле» на всех полях
      отвечает то же самое. Повторяют же обычно короче или телеграфно —
      «скутер в нячанге», «скутер нячанг», — и ни одного слова, которого не
      было, такой повтор не добавляет.

    Первая половина сравнивала ТРИ поля, и этого было мало до потери данных:
    «квартира в нячанге до 10000 долларов» → «квартира в нячанге до 1000
    долларов» отличается одной цифрой, разбор читал новый бюджет — а повтор
    решался по словам, где «1000» и «10000» сходились по общей основе. Правка
    клиента в десять раз молча выбрасывалась, и искали по прежней сумме. То же
    и с атрибутами: «нужен автомат» → «нужна механика» меняет запрос, а не
    формулировку.

    Сравнение НЕсимметрично, и это важно: учитывается только то, что новая
    формулировка САМА назвала. Повтор «ищу скутер в нячанге» после собранного
    ответа про бюджет бюджета не называет — и не должен обнулять его тем, что
    промолчал.
    """
    if (current.intent, current.category, current.city) != (
        fresh.intent,
        fresh.category,
        fresh.city,
    ):
        return False
    if _contradicts(current, fresh):
        return False
    return _adds_no_new_words(current.raw_query, fresh.raw_query)


def _contradicts(current: Passport, fresh: Passport) -> bool:
    """Назвала ли новая формулировка факт, отличный от собранного.

    Молчание фактом не считается: у полей, о которых новый текст ничего не
    сказал, остаётся прежнее значение — иначе любой короткий повтор стирал бы
    собранные ответы.
    """
    if _contradicts_budget(current.budget, fresh.budget):
        return True
    return any(
        key in current.attributes and current.attributes[key] != value
        for key, value in fresh.attributes.items()
    )


def _contradicts_budget(current: Budget, fresh: Budget) -> bool:
    """Назвала ли новая формулировка сумму, отличную от собранной.

    Поле за полем, и только те, которые новый текст НАЗВАЛ: «до 1000» после «до
    1000 долларов» валюту не называет, и молчание не должно читаться как «валюта
    другая». А вот «от 500» → «до 500» меняет запрос на противоположный, хотя
    отличается предлогом, — и ловится это здесь, потому что половина со словами
    предлоги не считает вовсе.

    Валюта — такое же поле: 300 донгов и 300 долларов не одна сумма, а разница
    в двадцать тысяч раз. Период не сравнивается: его ставит намерение
    («сниму» — помесячно), а не ответ про сумму.
    """
    return any(
        stated is not None and stated != collected
        for stated, collected in (
            (fresh.min, current.min),
            (fresh.max, current.max),
            (fresh.currency, current.currency),
        )
    )


def _adds_no_new_words(before: str, after: str) -> bool:
    """Все слова новой формулировки уже были в прежней.

    Не доля общих слов и не порог: доля симметрична и потому не отличает
    «сказал короче» от «заменил слово». «Ищу скутер в нячанге» → «ищу скутер в
    хойане» — три общих слова из пяти, то есть ровно 0.6, и повтор от смены
    города отделял бы знак сравнения. Замена же добавляет слово, которого не
    было, и этого достаточно — справочник городов здесь ни при чём.

    Асимметрия нарочная, но только по СОДЕРЖАТЕЛЬНЫМ словам: копипаст своей же
    фразы новых слов не приносит, а дописанное содержательное слово и есть новое
    сведение. Обещание «длиннее — новый запрос» без оговорки оказалось неверным:
    длиннее на один предлог покупало три новых вопроса (см. `_content_words`).

    Множества, а не последовательности: порядок клиент меняет свободно
    («скутер нячанг» и «нячанг скутер» — одно и то же). Слова сравниваются по
    основе (`_same_stem`), иначе «нячанг» и «нячанге» считались бы разными.

    Служебные слова и одиночные буквы не считаются (`_content_words`): без
    этого лимит трёх вопросов обходился приписыванием предлога — «ищу скутер в
    нячанге», потом «…а», потом «…а б», и каждый раз новая цепочка с новыми
    тремя вопросами (замерено: девять вопросов вместо трёх). Дописанное «с» в
    «квартира в нячанге с мебелью» — тот же запрос, а не новый.

    Чего этот критерий не ловит: перевод на другой язык («scooter in nha
    trang») и опечатку в ключевом слове — у них общих слов нет вовсе. Оба
    случая уходят в новую цепочку, и это осознанный предел, а не недосмотр:
    без нормализации написаний лечить их пришлось бы догадками. Остаётся и
    предел поменьше: приписывать можно не только предлоги, но и любую
    бессмыслицу из двух букв — она пройдёт как содержательное слово. Ограничен
    такой обход не здесь, а суточным потолком расходов у брокера.
    """
    known, said = _content_words(before), _content_words(after)
    if not known or not said:
        # Сравнивать нечего — считать это повтором нельзя: пустая формулировка
        # совпадает со всем подряд.
        return False
    return all(any(_same_stem(word, seen) for seen in known) for word in said)


def _same_stem(first: str, second: str) -> bool:
    """Одно слово в разных падежах: общая часть покрывает долю короткого слова.

    Числа сравниваются ТОЧНО и никогда по основе: «1000» и «10000» — не одна
    сумма в двух падежах, а разница в десять раз, и по общему префиксу они
    сходились. У числа нет морфологии, приравнивать его к слову нельзя.

    Слово короче нижнего порога совпадает только само с собой: у «в» и «до»
    вариантов написания нет, а сойтись по двум буквам они могли бы с чем
    угодно.
    """
    if first == second:
        return True
    if first.isdigit() or second.isdigit():
        return False
    shorter, longer = sorted((first, second), key=len)
    need = max(_STEM_MIN, ceil(len(shorter) * _STEM_SHARE))
    if len(shorter) < need:
        return False
    return longer.startswith(shorter[:need]) and shorter[:need] == longer[:need]


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.casefold()))


def _content_words(text: str) -> set[str]:
    """Слова, которые несут содержание просьбы.

    Отброшены служебные (`_FILLER_WORDS`) и одиночные буквы: ни «с», ни «а»,
    ни «б» не сообщают о предмете ничего, а как «новое слово» каждое из них
    обнуляло собранные ответы и счётчик вопросов.
    """
    return {word for word in _words(text) if len(word) > 1 and word not in _FILLER_WORDS}


def parse_option(field: str, raw: str) -> AnswerValue:
    """Значение кнопки → значение поля паспорта.

    Бюджет приезжает вместе с валютой («300 USD»), потому что подпись кнопки
    называет валюту, а значение обязано называть ту же: догадываться о ней по
    паспорту нельзя — там могут быть донги, и тогда «до 300 $» превратилось бы
    в 300 донгов.
    """
    if field == "budget.max":
        amount, _, currency = raw.partition(" ")
        return Budget(max=float(amount), currency=Currency(currency) if currency else None)
    if field == "attributes.rooms":
        return int(raw)
    return raw


# Клиент поправляет бота противопоставлением: «не 200000 VND, А обьем …»,
# «не скутер, А мотоцикл». Обе формы — из журнала бота (03.09.2026). Ключ здесь
# не отрицание само по себе («не важно» — это пропуск, а «недорого» вообще про
# цену), а именно пара «не X … а Y»: человек называет, что понято НЕ ТАК, и чем
# это заменить.
_CORRECTION_RE = re.compile(r"\bне\b.{0,80}?[,\s]\bа\b", re.IGNORECASE | re.DOTALL)


def corrects(text: str) -> bool:
    """Поправляет ли сообщение прежнюю просьбу, а не начинает новую.

    Разница дорогая. Поправка приносит содержательные слова («кубических»,
    «сантиметров»), поэтому `restates` отвечает `False` — и без этой проверки
    начиналась новая цепочка версий, в которой терялось всё, что человек уже
    сказал. Живой след 03.09.2026: «найди мне моцокил 200 кубиков» → бот прочёл
    200 000 VND бюджетом → «не 200000 VND, а обьем мощность двигателя до 200
    кубических сантиметров» → категория из первого сообщения исчезала, и бот
    спрашивал «Что ищем?» у человека, который только что всё объяснил.

    Почему именно противопоставление, а не отрицание. «Не важно» — пропуск,
    «недорого» — про цену, «не новый» — атрибут; ни одно из них не поправка.
    Поправку отличает пара «не X … а Y»: назвать неверно понятое и заменить его.

    Обратная сторона правила названа честно: «не скутер, а мотоцикл» тоже
    поправка, и это верно — человек меняет предмет в ТОЙ ЖЕ просьбе, а город и
    бюджет, которые он уже назвал, обязаны выжить. Цена ошибки в другую сторону
    (принять новый запрос за поправку) ограничена тем же противопоставлением:
    новый запрос его почти не содержит — «нужен мотоцикл» пишут без «не».

    Что делать с поправкой, решает не эта функция: слияние фактов уже есть —
    `search.refinements.merge_edit`, и второго такого знания в проекте быть
    не должно. Здесь только распознавание.
    """
    return _CORRECTION_RE.search(text) is not None


def apply_answer(passport: Passport, field: str, value: AnswerValue) -> Passport:
    """Ответ клиента → новая версия паспорта.

    Паспорт неизменяем (passport.md): здесь появляется новый объект, а версию
    ему присваивает репозиторий. Пропуск («не важно») сюда не доходит вовсе —
    он ничего не меняет и остаётся только событием.
    """
    update: dict[str, object] = {}
    if field == "budget.max":
        update["budget"] = _budget(passport, value)
    elif field == "category":
        update["category"] = Category.MOTORBIKE if value == "scooter" else Category(str(value))
        if value == "scooter":
            update["attributes"] = {
                **passport.attributes,
                "transmission": "automatic",
                "body_type": "tay_ga",
            }
    elif field == "city":
        update["city"] = str(value)
    elif field.startswith("attributes."):
        update["attributes"] = {**passport.attributes, field.removeprefix("attributes."): value}
    else:  # pragma: no cover — поля вне каталога вопросов сюда не приходят
        raise ValueError(f"поле {field!r} не заполняется ответом клиента")

    revised = passport.model_copy(update=update)
    return revised.model_copy(
        update={
            "missing_fields": [
                name for name in ("category", "city", "budget.max") if not has_value(revised, name)
            ],
            "status": PassportStatus.READY if revised.is_ready() else revised.status,
        }
    )


def _budget(passport: Passport, value: AnswerValue) -> Budget:
    """Ответ про сумму → бюджет. Валюта берётся у ответа, если он её назвал.

    Период не трогаем: его выбрал разбор запроса по намерению (аренда —
    помесячно, покупка — разово), и ответ про сумму об этом ничего не говорит.

    Нижняя граница сохраняется, но только пока она не спорит с новой верхней:
    «от 3 млн донгов» плюс кнопка «до 300 $» давали `min=3000000, max=300` —
    диапазон наизнанку, из которого никакой фильтр не соберётся. Проиграет
    старая граница: клиент только что назвал верхнюю, о ней он и говорил.
    """
    if isinstance(value, Budget):
        # Валюта ответа главнее паспортной: подпись кнопки её называет. Доллар
        # остаётся последним доводом — голое «500» без валюты приходит и от
        # клиента словами, и оно означает доллары, а не «валюта неизвестна».
        currency = value.currency or passport.budget.currency or Currency.USD
        top = value.max
    else:
        currency = passport.budget.currency or Currency.USD
        top = float(value)
    return Budget(
        min=_keep_floor(passport.budget, top, currency),
        max=top,
        currency=currency,
        period=passport.budget.period,
    )


def _keep_floor(current: Budget, top: float | None, currency: Currency | None) -> float | None:
    """Прежняя нижняя граница — если она всё ещё ниже верхней и в той же валюте."""
    floor = current.min
    if floor is None or top is None:
        return floor
    if current.currency is not None and current.currency != currency:
        # Границы в разных валютах — это не диапазон, а два разных числа.
        return None
    return floor if floor < top else None


def feedback_buttons(passport: Passport) -> tuple[Option, ...]:
    """Кнопки под выдачей. Зависят от паспорта: «нужен автомат» под квартирой — мусор."""
    buttons = [Option("дорого", Feedback.PRICEY.value), Option("не то", Feedback.WRONG.value)]
    if passport.category is Category.MOTORBIKE and not passport.attributes.get("transmission"):
        buttons.append(Option("нужен автомат", Feedback.AUTOMATIC.value))
    return tuple(buttons)


def apply_feedback(passport: Passport, kind: Feedback) -> Passport | None:
    """Обратная связь → новая версия паспорта. `None` — менять нечего, надо спросить."""
    if kind is Feedback.PRICEY:
        if not passport.budget.max:
            # Сколько «дорого» в цифрах, мы не знаем: бюджет не назывался.
            return None
        return apply_answer(passport, "budget.max", round(passport.budget.max * PRICEY_FACTOR))
    if kind is Feedback.AUTOMATIC:
        return apply_answer(passport, "attributes.transmission", "automatic")
    # «Не то» само по себе ничего не уточняет — это просьба спросить ещё раз.
    return None


def feedback_question(passport: Passport, kind: Feedback, asked: Sequence[str]) -> Question | None:
    """Что спросить, когда обратную связь не во что превратить.

    Лимит поднимается на один вопрос выше обычного: три вопроса — это защита от
    допроса до выдачи, а здесь клиент сам нажал кнопку и ждёт уточнения. Именно
    `MAX_FEEDBACK_QUESTIONS`, а не «на один больше уже заданных»: относительный
    потолок рос бы с каждым нажатием.
    """
    if kind is Feedback.PRICEY:
        return question_for("budget.max")
    return next_question(passport, asked, limit=MAX_FEEDBACK_QUESTIONS)


@dataclass(frozen=True, slots=True)
class DialogueState:
    """Сколько уже спросили и ждём ли ответ. Собирается из `passport_events`."""

    asked: tuple[str, ...] = ()
    pending: str | None = None


def advance(state: DialogueState, kind: str, payload: dict[str, object]) -> DialogueState:
    """Одно событие двигает состояние диалога."""
    if kind != EVENT_QUESTION_ASKED:
        # Любое другое событие — это реакция клиента: вопрос закрыт.
        return DialogueState(asked=state.asked, pending=None)
    field = str(payload.get("field") or "")
    if not field:  # pragma: no cover — событие без поля мы не пишем
        return state
    asked = state.asked if field in state.asked else (*state.asked, field)
    return DialogueState(asked=asked, pending=field)


def replay(events: Sequence[PassportEvent]) -> DialogueState:
    """Лог событий цепочки → состояние диалога.

    Состояние не хранится отдельной колонкой намеренно: оно выводится из
    истории, которую паспорт обязан вести и без диалога, и поэтому не может с
    ней разъехаться.
    """
    state = DialogueState()
    for event in events:
        state = advance(state, event.kind, dict(event.payload))
    return state
