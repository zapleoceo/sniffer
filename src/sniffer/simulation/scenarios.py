"""Что клиенты на самом деле пишут — и что бот на это обязан сделать.

Таблица, а не код: сценарий описан данными, потому что спорят здесь не о
реализации, а о том, какой диалог считать умным. Формулировки взяты из лога
владельца дословно там, где он есть («моцокил 200 кубиков», «нужен скутер, не
мотоцикл, honda lead»), и достроены по рынку Нячанга там, где его нет: два
обслуживаемых города и один необслуживаемый, три языка рынка, жильё и
транспорт, кнопка и ответ словами, повтор и смена темы.

`expect` — что обязано быть верно СЕГОДНЯ; сломается — это регресс.
`wish` — что должно быть верно, но пока нет: известный пробел с названной
причиной. Тесты держат их врозь (`wish` идёт под `xfail`), иначе выбор был бы
между зелёным прогоном и честным описанием качества.

Устройство сценария — в `script.py`.
"""

from __future__ import annotations

from sniffer.domain.dialogue import SKIP, Feedback
from sniffer.simulation.script import Reacts, Says, Scenario, Taps, Wish

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="scooter_lead",
        title="нужен скутер, не мотоцикл, honda lead",
        steps=(Says("нужен скутер, не мотоцикл, honda lead"),),
        # Живой отказ 02.09.2026: бот отдал три Airblade, потому что «lead»
        # читалось маркой и дальше не участвовало нигде. Модель обязана быть
        # своим полем, а коробка — следовать из неё: Lead всегда автомат, и
        # спрашивать про неё незачем (passport.md, «Марка и модель»).
        expect={
            "category": "motorbike",
            "city": "nha_trang",
            "attributes.brand": "honda",
            "attributes.model": "lead",
            "attributes.transmission": "automatic",
        },
        forbid=("budget.max",),
    ),
    Scenario(
        key="typo_200cc",
        title="найди мне моцокил 200 кубиков (опечатка + объём)",
        steps=(Says("найди мне моцокил 200 кубиков"), Taps("motorbike")),
        max_questions_before_results=1,
        expect={"attributes.engine_cc": 200, "category": "motorbike"},
        # Ради этой строки харнес и написан: 200 кубиков стоили клиента дважды.
        forbid=("budget.max",),
    ),
    Scenario(
        key="scooter_nha_trang",
        title="ищу скутер в Нячанге",
        steps=(Says("ищу скутер в Нячанге"),),
        expect={"category": "motorbike", "city": "nha_trang", "intent": "buy"},
    ),
    Scenario(
        key="apartment_10m",
        title="квартиру снять до 10 млн",
        steps=(Says("квартиру снять до 10 млн"),),
        expect={
            "category": "apartment",
            "intent": "rent",
            "budget.max": 10_000_000.0,
            "budget.currency": "VND",
        },
    ),
    Scenario(
        key="hello",
        title="привет (ни категории, ни запроса)",
        steps=(Says("привет"),),
        max_questions_before_results=1,
        expect_results=None,
        expect_text="Что ищем",
    ),
    Scenario(
        key="what_is_there",
        title="что тут есть → «не важно»",
        steps=(Says("что тут есть"), Taps(SKIP)),
        max_questions_before_results=1,
        expect_text="Ищу",
    ),
    Scenario(
        key="brand_without_category",
        title="honda до 300 (марка без категории)",
        steps=(Says("honda до 300"),),
        max_questions_before_results=1,
        expect_results=None,
        expect={"attributes.brand": "honda", "budget.max": 300.0, "budget.currency": "USD"},
    ),
    Scenario(
        key="unserved_city",
        title="ищу скутер в Хойане (город, где мы не ищем)",
        steps=(Says("ищу скутер в Хойане"),),
        expect_results=False,
        expect={"city": "hoi_an", "category": "motorbike"},
        expect_text="Хойан",
    ),
    Scenario(
        key="apartment_furnished",
        title="нужна квартира с мебелью на длительный срок",
        steps=(Says("нужна квартира с мебелью на длительный срок"),),
        expect={"category": "apartment", "intent": "rent"},
    ),
    Scenario(
        key="scooter_automatic_500",
        title="скутер автомат до 500 долларов",
        steps=(Says("скутер автомат до 500 долларов"),),
        expect={"category": "motorbike", "budget.max": 500.0, "budget.currency": "USD"},
        wish=Wish(
            fields={"attributes.transmission": "automatic"},
            why=(
                "коробка в первичном запросе не разбирается: intake_rules читает "
                "марку и объём, но не трансмиссию. Клиент сказал «автомат», а бот "
                "предлагает под карточками кнопку «нужен автомат» — спрашивает то, "
                "что уже услышал"
            ),
        ),
    ),
    Scenario(
        key="motorbike_manual_exciter",
        title="мотоцикл механика exciter",
        steps=(Says("мотоцикл механика exciter"),),
        # Exciter — модель Yamaha, и марка обязана выводиться ИЗ неё, а не
        # совпадать с ней: пока модели и марки лежали одним списком, паспорт
        # получал brand=exciter, а производителя не знал никто.
        expect={
            "category": "motorbike",
            "attributes.brand": "yamaha",
            "attributes.model": "exciter",
            "attributes.transmission": "manual",
        },
    ),
    Scenario(
        key="repeat_same_words",
        title="повтор той же фразы слово в слово",
        steps=(Says("ищу скутер в нячанге"), Says("ищу скутер в нячанге")),
        expect={"category": "motorbike", "city": "nha_trang"},
    ),
    Scenario(
        key="topic_switch",
        title="смена темы посреди диалога",
        steps=(Says("ищу скутер в нячанге"), Says("ладно, тогда квартиру до 10 млн")),
        expect={"category": "apartment", "budget.max": 10_000_000.0, "intent": "rent"},
    ),
    Scenario(
        key="words_instead_of_button",
        title="ответ словами вместо кнопки",
        steps=(Says("привет"), Says("скутер")),
        max_questions_before_results=1,
        expect={"category": "motorbike"},
    ),
    Scenario(
        key="pricey_after_results",
        title="«дорого» после выдачи",
        steps=(Says("скутер honda до 500"), Reacts(Feedback.PRICEY)),
        expect={"category": "motorbike", "budget.max": 350.0},
    ),
    Scenario(
        key="wrong_after_results",
        title="«не то» после выдачи",
        steps=(Says("ищу скутер в нячанге"), Reacts(Feedback.WRONG)),
        expect={"category": "motorbike"},
        expect_text="бюджет",
    ),
    Scenario(
        key="bare_budget",
        title="голый бюджет без предмета: до 400",
        steps=(Says("до 400"),),
        max_questions_before_results=1,
        expect_results=None,
        expect={"budget.max": 400.0, "budget.currency": "USD"},
    ),
    Scenario(
        key="scooter_da_nang",
        title="ищу скутер в Дананге (второй обслуживаемый город)",
        steps=(Says("ищу скутер в Дананге"),),
        expect={"category": "motorbike", "city": "da_nang"},
    ),
    Scenario(
        key="room_cheap",
        title="сниму комнату недорого",
        steps=(Says("сниму комнату недорого"),),
        expect={"category": "room", "intent": "rent"},
    ),
    Scenario(
        key="english_scooter",
        title="looking for a scooter in nha trang",
        steps=(Says("looking for a scooter in nha trang"),),
        expect={"category": "motorbike", "city": "nha_trang"},
    ),
    Scenario(
        key="vietnamese_bike",
        title="xe máy honda nha trang",
        steps=(Says("xe máy honda nha trang"),),
        expect={"category": "motorbike", "city": "nha_trang", "attributes.brand": "honda"},
    ),
    Scenario(
        key="cyrillic_brand",
        title="ищу хонду вижн до 400 (марка кириллицей)",
        steps=(Says("ищу хонду вижн до 400"),),
        max_questions_before_results=1,
        expect_results=None,
        expect={
            "budget.max": 400.0,
            # Русскоязычный Нячанг пишет марку кириллицей — «хонда», «вижн», —
            # и разбор это уже умеет.
            "attributes.brand": "honda",
            "attributes.model": "vision",
        },
        wish=Wish(
            fields={"category": "motorbike"},
            why=(
                "модель узнана (vision), а категория — нет, и бот спрашивает "
                "«что ищем?». Vision — мотобайк по самому модельному ряду, категория "
                "следует из него однозначно. Вопрос там, где предмет уже назван, — "
                "это ровно та «тупизна», на которую жаловался владелец"
            ),
        ),
    ),
)
