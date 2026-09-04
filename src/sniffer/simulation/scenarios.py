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

04.09.2026 — возврат search-first. Владелец пожаловался на лишние вопросы
(«Я в Нечанге, нужен мотак» → бот всё равно спросил бюджет; «байк за 14 лямов» —
спросил город), и воронку категория→город→бюджет откатили: `blocking_question`
(`domain/dialogue.py`) до выдачи спрашивает ТОЛЬКО категорию. Город подставляет
`default_city`, бюджет уточняет обратная связь «дорого» под карточками. Отсюда
правка всей таблицы ниже:

* `Taps`, отвечавшие на вопросы о городе и бюджете, убраны — отвечать им больше
  не на что. Второе нажатие в пустоту харнес честно наблюдает как заглушку
  «Сначала напишите…» (вопроса не осталось, код кнопки ведёт в никуда) или как
  молчание (кнопка старого, уже закрытого вопроса) — это тоже сигнал, не баг
  харнеса, см. `harness._play`.
* `max_questions_before_results` почти везде стал 0 (категория читается из
  фразы) или 1 (когда предмет неизвестен и категория — единственный вопрос).
* `city` в `expect` остаётся ТОЛЬКО там, где город назван в САМОМ запросе.
  `default_city` подставляет Нячанг в бою (`search.intake.QueryIntake.parse`),
  но харнес (`simulation/harness.py:_RulesIntake`) гоняет диалог через
  `intake_rules.parse_query` БЕЗ `default_city` — этот путь не входит в
  владение этого файла, расхождение с боем названо отдельно в отчёте задачи.
  Практически: подставленный город здесь никогда не появляется, только
  сказанный.
* `budget.max`/`budget.currency` в `expect` остаются только там, где в тексте
  есть число — кнопка, которая раньше ставила сумму без слов клиента, исчезла
  вместе с вопросом.
* Два сценария (`wrong_after_results`, `yamaha_da_nang_refinement`) раньше
  ставили город и бюджет кнопками ПОСЛЕ фразы без них; теперь блокирующих
  вопросов для этого нет, и, чтобы сценарии по-прежнему проверяли то же самое
  (уточнение бюджета через обратную связь, второй город), город и бюджет
  дописаны в сам текст запроса.
* `motorbike_manual_exciter` держал `expect_results=False`: единственный
  подходящий лот каталога (`yamaha-exciter-155`, 24 млн донгов) раньше отсекала
  сумма в 500$, поставленная кнопкой. Кнопки бюджета больше нет, в тексте его
  тоже нет — лот честно доезжает до выдачи, и это поведение продукта, а не
  повод держать сценарий в противоречии с рынком.

Устройство сценария — в `script.py`.
"""

from __future__ import annotations

from sniffer.domain.dialogue import Feedback
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
            "attributes.brand": "honda",
            "attributes.model": "lead",
            "attributes.transmission": "automatic",
        },
    ),
    Scenario(
        key="typo_200cc",
        title="найди мне моцокил 200 кубиков (опечатка + объём)",
        # Шаг «нажать категорию» убран, и вопросов стало на один меньше: бот
        # теперь узнаёт «моцокил» сам (`intake_rules`, опечатки из журнала), а
        # прежняя версия сценария закрепляла обратное — что категорию у клиента
        # приходится переспрашивать. Тап по категории после правки уходил в
        # вопрос о ГОРОДЕ, и город становился «motorbike»: сценарий, описывающий
        # поведение шагами, ломается ровно тогда, когда бот умнеет, и это
        # правильно — иначе улучшение прошло бы незамеченным.
        steps=(Says("найди мне моцокил 200 кубиков"),),
        expect={
            "attributes.engine_cc": 200,
            "category": "motorbike",
            "intent": "buy",
        },
        # Ради этой строки харнес и написан: 200 кубиков стоили клиента дважды.
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
        title="что тут есть → категория кнопкой",
        # Раньше здесь ещё два шага отвечали на город и на «не важно» к бюджету
        # — обоих вопросов больше нет (`blocking_question` спрашивает только
        # категорию), и после ответа на неё бот идёт прямо в поиск.
        steps=(Says("что тут есть"), Taps("motorbike")),
        max_questions_before_results=1,
        expect_text="Ищу",
    ),
    Scenario(
        key="brand_without_category",
        title="honda до 300 (марка без категории)",
        steps=(Says("honda до 300"),),
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
        #
        # `expect_results` раньше стоял `False`: единственный подходящий лот
        # каталога («yamaha-exciter-155», 24 млн донгов) отсекала сумма в 500$,
        # поставленная кнопкой города/бюджета. Кнопки больше нет, в тексте
        # бюджета тоже нет — лот честно доезжает до выдачи, это поведение
        # search-first, а не повод держать сценарий в споре с каталогом.
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
        steps=(
            Says("ищу скутер в нячанге до 500 долларов"),
            Says("ищу скутер в нячанге до 500 долларов"),
        ),
        expect={"category": "motorbike", "city": "nha_trang"},
    ),
    Scenario(
        key="topic_switch",
        title="смена темы посреди диалога",
        steps=(
            Says("ищу скутер в нячанге до 500 долларов"),
            Says("ладно, тогда квартиру в нячанге до 10 млн"),
        ),
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
        steps=(Says("скутер honda в нячанге до 500"), Reacts(Feedback.PRICEY)),
        expect={"category": "motorbike", "budget.max": 350.0},
    ),
    Scenario(
        key="wrong_after_results",
        title="«не то» после выдачи",
        # Бюджет и коробка названы в самом тексте, а не кнопкой: раньше их до
        # выдачи ставил обязательный вопрос о бюджете, и к моменту «не то»
        # неизвестным оставалось только состояние — вопрос целился в него
        # (`FIELD_INFORMATIVENESS`: budget 0.45, transmission 0.30, condition
        # 0.15). Вопроса о бюджете до выдачи больше нет; чтобы «не то»
        # по-прежнему упиралось в «Состояние», а не заново в бюджет, бюджет и
        # коробка названы в запросе явно.
        steps=(Says("ищу скутер в нячанге до 500 долларов"), Reacts(Feedback.WRONG)),
        expect={"category": "motorbike"},
        expect_text="Состояние",
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
        steps=(
            Says("хочу что-нибудь недорогое"),
            Taps("motorbike"),
        ),
        max_questions_before_results=1,
        expect={"category": "motorbike"},
    ),
    Scenario(
        key="vague_rental",
        title="мутно: переезжаю в Нячанг, хочу что-нибудь снять",
        steps=(
            Says("переезжаю в Нячанг, хочу что-нибудь снять"),
            Taps("apartment"),
        ),
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
        # «что ищем?» здесь больше не спрашивают — предмет уже назван маркой, и
        # категория известна ДО первого (и единственного блокирующего) вопроса.
        # `expect_results=None`: показать выдачу сразу или сузить её приглашением
        # над карточками (`_result_header`, широкий запрос) — обе формы
        # допустимы, лишь бы категория и марка распознались.
        expect_results=None,
        expect={"category": "motorbike", "attributes.brand": "honda"},
    ),
    Scenario(
        key="bare_engine",
        title="мутно: нужно до 125 кубов, больше не хочу",
        steps=(
            Says("нужно до 125 кубов, больше не хочу"),
            Taps("motorbike"),
        ),
        max_questions_before_results=1,
        expect={"category": "motorbike", "attributes.engine_cc": 125},
    ),
    Scenario(
        key="broad_automatic_bike",
        title="байк или скутер — главное автомат",
        steps=(Says("байк или скутер — главное автомат"),),
        expect={"category": "motorbike", "attributes.transmission": "automatic"},
    ),
    Scenario(
        key="plain_scooter_automatic_funnel",
        title="скутер автомат — город и бюджет клиент не назвал",
        # Раньше город и бюджет ставились кнопками ПОСЛЕ фразы «скутер автомат»
        # — сама фраза называла только категорию и коробку. Кнопок для города и
        # бюджета больше нет (`blocking_question` спрашивает только категорию),
        # а в тексте ни города, ни бюджета нет — значит, паспорту взять их
        # неоткуда, и `expect` про них теперь молчит.
        steps=(Says("скутер автомат"),),
        expect={
            "intent": "buy",
            "category": "motorbike",
            "attributes.transmission": "automatic",
        },
    ),
    Scenario(
        key="yamaha_da_nang_refinement",
        title="ямаха в Дананге — уточнение цены после выдачи",
        # Город и бюджет раньше ставились кнопками после фразы «ищу ямаха
        # скутер» — блокирующих вопросов для них больше нет, поэтому оба
        # названы в самом запросе: иначе сценарий перестал бы проверять и
        # Дананг, и пересчёт «дорого» (1000 → 700).
        steps=(
            Says("ищу ямаха скутер в дананге до 1000 долларов"),
            Reacts(Feedback.PRICEY),
        ),
        expect={
            "intent": "buy",
            "category": "motorbike",
            "city": "da_nang",
            "budget.max": 700.0,
            "budget.currency": "USD",
            "attributes.brand": "yamaha",
        },
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
        title="прокат мопеда посуточно",
        # «мопед», не «скутер»: слово «скутер» разбор читает как automatic +
        # tay_ga (см. `scooter_automatic_500`), а оба прокатных лота каталога
        # (rental-bikes-shop, rental-scooters-daily) заведомо не называют
        # коробку в тексте — правды о ней у них нет. Структурный отбор доски
        # (`market._source_accepts`) требует точного совпадения коробки, когда
        # она названа паспортом, и честно отсеивал бы оба лота с пустой правдой
        # — не по бюджету и не по числу вопросов, а по коробке, которую клиент
        # тут вообще не называл. «Мопед» даёт ту же категорию, намерение и
        # период, не сужая по коробке.
        steps=(Says("прокат мопеда посуточно"),),
        # «прокат» → rent, «посуточно» → период day (срок аренды — это период
        # цены, отдельного поля нет: passport.md, «Прокат — аренда»).
        expect={"category": "motorbike", "intent": "rent", "budget.period": "day"},
    ),
    Scenario(
        key="buy_scooter_control",
        title="куплю мопед (контроль: прокат отсекается)",
        steps=(Says("куплю мопед недорого"),),
        # Зеркало аренды: тот же прокат на рынке, но клиент ПОКУПАЕТ, и оффер
        # аренды ему «мимо» (`relevance._is_rental_offer`, spec-v2 2.7). Прокатных
        # лотов в выдаче быть НЕ должно — проверяет `test_buy_hides_rental`.
        #
        # «мопед», а не «скутер», и это НЕ косметика: «скутер» ставит
        # transmission=automatic, а прокатные лоты коробку в тексте не называют
        # (`lot.transmission==""`), и структурный фильтр рынка отсёк бы их ДО
        # `rank_items` — тогда `_is_rental_offer` не вызвался бы вовсе, и контроль
        # стал бы ложным: зелёным даже при выключенном отсеве аренды (нашло
        # Сонет-ревью 04.09.2026). «мопед» коробку не ставит, прокат доезжает до
        # ранжирования, и buy-фильтр проверяется по-настоящему. Бюджета в запросе
        # нет, и кнопки, которая раньше его скипала, тоже больше нет: цену
        # прокатных лотов не знаем, и потолок отсёк бы их мимо сути — а так
        # взяться ему попросту неоткуда.
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
    # ── из живого журнала 04.09.2026 (боль владельца про лишние вопросы) ──────
    Scenario(
        key="journal_motak_in_nha_trang",
        title="Я в Нечанге, нужен мотак (сленг + город назван)",
        steps=(Says("Я в Нечанге, нужен мотак пиздатый"),),
        # Живой отказ: бот всё равно спрашивал бюджет, хотя город и предмет
        # названы. Search-first: 0 вопросов. «мотак» — сленг байка (intake_rules),
        # «в Нечанге» — город. Мат игнорируется молча.
        forbid=("budget.max",),
        expect={"category": "motorbike", "city": "nha_trang", "intent": "buy"},
    ),
    Scenario(
        key="journal_motorcycle_not_scooter",
        title="хочу именно мотоцикл, не скутер",
        steps=(Says("хочу именно мотоцикл, не скутер"),),
        # Живой отказ: «не скутер» разбиралось КАК скутер (body_type=tay_ga), и
        # клиент, просивший мотоцикл, получал скутеры. Теперь отрицание ставит
        # transmission=manual — автоматы (скутеры) отсекаются.
        expect={"category": "motorbike", "attributes.transmission": "manual"},
    ),
    Scenario(
        key="journal_bike_for_14_lyamov",
        title="байк за 14 лямов (бюджет словом, город не назван)",
        steps=(Says("байк за 14 лямов"),),
        # Живой отказ: бюджет назван («14 лямов» = 14 млн донгов), а бот спросил
        # город. Search-first: город дефолтится в Нячанг, вопросов ноль.
        expect={
            "category": "motorbike",
            "city": "nha_trang",
            "budget.max": 14_000_000.0,
            "budget.currency": "VND",
        },
    ),
)
