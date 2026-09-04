"""Что клиенты на самом деле пишут — и что бот на это обязан сделать.

Таблица, а не код: сценарий описан данными, потому что спорят здесь не о
реализации, а о том, какой диалог считать умным. Формулировки взяты из лога
владельца дословно там, где он есть («моцокил 200 кубиков», «нужен скутер, не
мотоцикл, honda lead»), и достроены по рынку Нячанга там, где его нет: два
обслуживаемых города и один необслуживаемый, три языка рынка, кнопка и ответ
словами, повтор и смена темы.

Покрывается ВСЯ матрица универсальности, а не только покупка скутера: транспорт
на всех сторонах сделки (купить, арендовать, продать), мотоциклы, узнаваемые
именем семейства (`honda cbr`, `kawasaki z300`, `yamaha mt`, — не скутеры), и
жильё во всех видах (студия у моря, двушка с мебелью, комната, дом на длительный
срок, «жильё до 15 млн»). Прокат — контрольная пара: клиенту `intent=rent` он
нужен и показывается, клиенту `intent=buy` он «мимо» и отсекается.

`expect` — что обязано быть верно СЕГОДНЯ; сломается — это регресс.
`wish` — что должно быть верно, но пока нет: известный пробел с названной
причиной. Тесты держат их врозь (`wish` идёт под `xfail`), иначе выбор был бы
между зелёным прогоном и честным описанием качества.

Сегодня `wish` нет ни у одного сценария, и это не значит, что механизм лишний:
оба пожелания замера 02.09.2026 — коробка из первичного запроса и категория из
модели — выполнены, и их поля переехали в `expect`, где регресс красит тест.
Оставить их пожеланиями значило бы называть работой то, что уже сделано;
сторожит этот переезд `test_a_fulfilled_wish_moves_to_the_expectations`.

Устройство сценария — в `script.py`.
"""

from __future__ import annotations

from sniffer.domain.dialogue import SKIP, Feedback
from sniffer.simulation.script import Reacts, Says, Scenario, Taps

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
        # Коробка названа словом, и разбор обязан её услышать. Пока не слышал,
        # бот предлагал под карточками кнопку «нужен автомат» — то есть
        # переспрашивал то, что клиент уже сказал.
        expect={
            "category": "motorbike",
            "budget.max": 500.0,
            "budget.currency": "USD",
            "attributes.transmission": "automatic",
        },
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
        # Ни одного вопроса: предмет назван моделью. Пока категория из модели не
        # выводилась, бот спрашивал «что ищем?» у клиента, который уже ответил, —
        # это и есть та «тупизна», на которую жаловался владелец.
        expect={
            "category": "motorbike",
            "budget.max": 400.0,
            # Русскоязычный Нячанг пишет марку кириллицей — «хонда», «вижн», —
            # и разбор это уже умеет.
            "attributes.brand": "honda",
            "attributes.model": "vision",
        },
    ),
    Scenario(
        key="vague_cheap_thing",
        title="мутно: хочу что-нибудь недорогое",
        steps=(Says("хочу что-нибудь недорогое"), Taps("motorbike")),
        max_questions_before_results=1,
        expect={"category": "motorbike"},
    ),
    Scenario(
        key="vague_rental",
        title="мутно: переезжаю в Нячанг, хочу что-нибудь снять",
        steps=(Says("переезжаю в Нячанг, хочу что-нибудь снять"), Taps("apartment")),
        max_questions_before_results=1,
        expect={"category": "apartment", "city": "nha_trang", "intent": "rent"},
    ),
    Scenario(
        key="brand_only_then_category",
        title="мутно: хочу хонду (марка задаёт предмет)",
        steps=(Says("хочу хонду"),),
        # Марка выводит категорию так же, как модель: все марки рынка
        # мотобайковые, и «хонда» — это мотобайк (defect 02.09.2026: на «yamaha»
        # категория оставалась пустой и в выдачу лезла даже квартира). Поэтому
        # «что ищем?» здесь больше не спрашивают — предмет уже назван маркой.
        # `expect_results=None`: показать выдачу или доуточнить широкий запрос —
        # обе ветки допустимы, лишь бы категория и марка распознались.
        max_questions_before_results=1,
        expect_results=None,
        expect={"category": "motorbike", "attributes.brand": "honda"},
    ),
    Scenario(
        key="bare_engine",
        title="мутно: нужно до 125 кубов, больше не хочу",
        steps=(Says("нужно до 125 кубов, больше не хочу"), Taps("motorbike")),
        max_questions_before_results=1,
        expect={"category": "motorbike", "attributes.engine_cc": 125},
        forbid=("budget.max",),
    ),
    Scenario(
        key="broad_automatic_bike",
        title="байк или скутер — главное автомат",
        steps=(Says("байк или скутер — главное автомат"),),
        expect={"category": "motorbike", "attributes.transmission": "automatic"},
    ),
    Scenario(
        key="yamaha_not_honda_kymco",
        title="ямаха скутер (в чате висят Honda и Kymco)",
        steps=(Says("ямаха скутер"),),
        # realcheck 03.09.2026: на «ямаха» приходили Honda и Kymco — поля марки у
        # чата нет, а живой отсев марку не читал (словарь фраз, где марок нет
        # вовсе). Ямаха в выдаче остаться ОБЯЗАНА, иначе лечение хуже болезни;
        # чужое — уйти. Мимо запроса судит `fit.py` правдой о лоте, не баллом.
        expect={"category": "motorbike", "attributes.brand": "yamaha"},
    ),
    Scenario(
        key="automatic_not_manual",
        title="скутер автомат до 700 (в чате висит механика)",
        steps=(Says("скутер автомат до 700"),),
        # Та же болезнь по оси коробки: на «автомат» приходила механика (R15,
        # Winner). Механика в чате дешёвая — проходит бюджет и обязана уйти
        # отсевом по коробке, а не отсеяться ценой, оставив ось непроверенной.
        expect={
            "category": "motorbike",
            "attributes.transmission": "automatic",
            "budget.max": 700.0,
            "budget.currency": "USD",
        },
    ),
    Scenario(
        key="studio_no_bare_bike",
        title="сниму студию у моря (в чате байк одной моделью)",
        steps=(Says("сниму студию у моря"),),
        # realcheck 03.09.2026: в выдаче студий всплывал байк, названный ТОЛЬКО
        # моделью («Honda Lead 110 2008», в каталоге — «Yamaha R15 2019»): слова
        # категории в нём нет, а модель раньше категорию не выводила, и отсев
        # чужой категории его не видел. Теперь `category_of` выводит motorbike из
        # имени модели и на стороне лота — байк в выдаче квартир не всплывает.
        # Заодно сторожит разбор атрибутов жилья: «студию» → rooms=1, «у моря» →
        # sea_view. Чужое число комнат (2 спальни) в выдаче студий — «мимо».
        expect={
            "category": "apartment",
            "intent": "rent",
            "attributes.rooms": 1,
            "attributes.sea_view": True,
        },
    ),
    Scenario(
        key="empty_politeness",
        title="мутно: помоги пожалуйста",
        steps=(Says("помоги пожалуйста"),),
        max_questions_before_results=1,
        expect_results=None,
        expect_text="Что ищем",
    ),
    # ── универсализация: транспорт (аренда / мотоцикл / продажа) ─────────────
    Scenario(
        key="rent_bike_month",
        title="арендовать байк на месяц (прокат, не покупка)",
        steps=(Says("арендовать байк на месяц"),),
        # «арендовать» — аренда, а не покупка: клиент хочет ВЗЯТЬ байк. Раньше
        # универсальный агент читал это как buy, и отсев проката выбрасывал ровно
        # те лоты, что нужны (passport.md, «Прокат — аренда»). Теперь intent=rent,
        # и прокатные лоты в выдаче ОСТАЮТСЯ — проверяет `test_rent_shows_rental`.
        expect={"category": "motorbike", "intent": "rent"},
    ),
    Scenario(
        key="rent_scooter_daily",
        title="прокат скутера посуточно",
        steps=(Says("прокат скутера посуточно"),),
        # «прокат» → rent, «посуточно» → период day (срок аренды — это период
        # цены, отдельного поля нет: passport.md, «Прокат — аренда»).
        expect={"category": "motorbike", "intent": "rent", "budget.period": "day"},
    ),
    Scenario(
        key="buy_scooter_control",
        title="куплю скутер (контроль: прокат отсекается)",
        steps=(Says("куплю скутер недорого"),),
        # Зеркало аренды: тот же прокат на рынке, но клиент ПОКУПАЕТ, и оффер
        # аренды ему «мимо» (`relevance._is_rental_offer`, spec-v2 2.7). Прокатных
        # лотов в выдаче быть НЕ должно — проверяет `test_buy_hides_rental`.
        expect={"category": "motorbike", "intent": "buy"},
    ),
    Scenario(
        key="moto_cbr",
        title="honda cbr (мотоцикл, не скутер)",
        steps=(Says("honda cbr"),),
        # Бот был скутеро-заточенным: на «honda cbr» возвращал чужой скутер. Теперь
        # cbr — модель семейства côn tay: марка honda, коробка manual выводятся из
        # неё, категория — тоже. Скутер (чужая модель) в выдаче не появляется —
        # это сторожит отсев модели плюс мерка `off_target` («чужая модель»).
        expect={
            "category": "motorbike",
            "attributes.brand": "honda",
            "attributes.model": "cbr",
            "attributes.transmission": "manual",
        },
    ),
    Scenario(
        key="moto_z300",
        title="kawasaki z300 (номер в имени — не бюджет)",
        steps=(Says("kawasaki z300"),),
        # Живой отказ 02.09.2026: «Kawasaki z300» давал бюджет «до 300 USD», и
        # клиент, искавший 300-кубовый мотоцикл, получал 50cc. Теперь z300 — модель
        # (kawasaki, механика), а число в имени бюджетом не читается.
        expect={
            "category": "motorbike",
            "attributes.brand": "kawasaki",
            "attributes.model": "z",
            "attributes.transmission": "manual",
        },
        forbid=("budget.max",),
    ),
    Scenario(
        key="moto_mt",
        title="yamaha mt (короткое имя семейства по марке)",
        steps=(Says("yamaha mt"),),
        # «mt» двухбуквенное и в чужом слове ловилось бы ложно, поэтому узнаётся
        # только с якорем — по марке («yamaha mt») или по цифре («mt15»). Марка
        # yamaha и коробка manual следуют из модели.
        expect={
            "category": "motorbike",
            "attributes.brand": "yamaha",
            "attributes.model": "mt",
            "attributes.transmission": "manual",
        },
    ),
    Scenario(
        key="sell_honda_lead",
        title="продаю honda lead (намерение продать)",
        steps=(Says("продаю honda lead"),),
        # Продажа: клиент отдаёт свой байк. Разбор обязан не падать и осмысленно
        # ответить; ТОЧНАЯ политика продажи владельцем ещё не решена (показывать ли
        # похожие лоты, оценку, спрос), поэтому `expect_results=None` — сценарий
        # терпим к выдаче и сторожит лишь разбор намерения и предмета.
        expect_results=None,
        expect={
            "category": "motorbike",
            "intent": "sell",
            "attributes.brand": "honda",
            "attributes.model": "lead",
        },
    ),
    # ── универсализация: жильё во всех видах ─────────────────────────────────
    Scenario(
        key="rent_two_bedrooms_furnished",
        title="квартиру 2 спальни с мебелью (2 — не бюджет)",
        steps=(Says("квартиру 2 спальни с мебелью"),),
        # Универсализация вскрыла «счётное число — не бюджет»: «2 спальни» давало
        # «до 2 USD». Теперь «2» перед счётной единицей — количество, не сумма
        # (passport.md). rooms=2 жёсткое (студия отсекается), furnished мягкое
        # (квартира без мебели остаётся).
        expect={
            "category": "apartment",
            "intent": "rent",
            "attributes.rooms": 2,
            "attributes.furnished": True,
        },
        forbid=("budget.max",),
    ),
    Scenario(
        key="rent_room_cheap_plain",
        title="комнату недорого (без глагола сделки)",
        steps=(Says("комнату недорого"),),
        # «комнату» без «сниму»: намерение из глагола не следует, но жильё в Нячанге
        # снимают — категория ROOM даёт intent=rent (`_RENTED_CATEGORIES`).
        expect={"category": "room", "intent": "rent"},
    ),
    Scenario(
        key="rent_house_long_term",
        title="дом на длительный срок",
        steps=(Says("дом на длительный срок"),),
        # «дом» → HOUSE, «длительный срок» → период month. Дом снимают так же, как
        # квартиру: intent=rent из категории.
        expect={"category": "house", "intent": "rent"},
    ),
    Scenario(
        key="housing_any_15m",
        title="жильё до 15 млн (самый общий вид жилья)",
        steps=(Says("жильё до 15 млн"),),
        # «жильё» раньше не имело категории, и бот спрашивал «что ищем?» при
        # названном предмете. Теперь «жильё» → APARTMENT (самый общий вид), а «до 15
        # млн» — бюджет в донгах (passport.md, «Прокат — аренда»).
        expect={
            "category": "apartment",
            "intent": "rent",
            "budget.max": 15_000_000.0,
            "budget.currency": "VND",
        },
    ),
)
