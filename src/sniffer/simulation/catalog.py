"""Каталог поддельного рынка Нячанга: находки вместе с правдой о них.

Подделка нарочно грязная. Отдай она только идеальные совпадения — харнес мерил
бы себя: любая выдача выглядела бы безупречной, а «показывает не то» из жалобы
владельца не воспроизвелось бы никогда. Поэтому здесь лежат ровно те находки,
из-за которых на выдачу и жалуются: Airblade на запрос Lead, лот 59 дней от
роду, вариант вдвое дороже названного бюджета и чужая категория, которую живой
поиск по чату вытаскивает по одному общему слову.

Правда о лоте (марка, модель, коробка, объём) лежит РЯДОМ с `RawItem`, а не
внутри него: источник таких полей не отдаёт — он отдаёт текст. Это знание
симулятора, и смешивать его с тем, что видит бот, нельзя, иначе бот получит
подсказку, которой в бою нет. Судят этой правдой в `fit.py`, отдельно: мерку не
должно быть можно поправить тем же движением, что каталог.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sniffer.domain.passport import Category
from sniffer.sources.base import RawItem


@dataclass(frozen=True, slots=True)
class Lot:
    """Находка плюс правда о ней. Боту достаётся только `item`.

    `rooms` и `rental` — правда жилья и проката, добавленная универсализацией.
    Число комнат жёсткое, как модель и объём (`fit._wrong_rooms`); `rental`
    метит оффер аренды — он «мимо» покупателю (`intent=BUY`), но нужен
    арендатору (`intent=RENT`), ровно как в отсеве перед показом (spec-v2 2.7).
    Мебель и вид на море правдой рядом НЕ лежат: они мягкие сигналы балла, а не
    отсева, и мерке судить их нечем — их знание остаётся только в тексте лота.
    """

    item: RawItem
    category: Category
    brand: str = ""
    model: str = ""
    transmission: str = ""
    engine_cc: int | None = None
    rooms: int | None = None
    rental: bool = False


def _lot(
    key: str,
    title: str,
    *,
    category: Category,
    price_vnd: int,
    age_days: int,
    source: str = "chotot",
    brand: str = "",
    model: str = "",
    transmission: str = "",
    engine_cc: int | None = None,
    rooms: int | None = None,
    rental: bool = False,
) -> Lot:
    url = (
        f"https://www.chotot.com/{key}.htm"
        if source == "chotot"
        else f"https://t.me/nhatrang_baraholka/{key}"
    )
    return Lot(
        item=RawItem(
            source=source,
            external_id=key,
            url=url,
            title=title,
            price_raw=f"{price_vnd:,} đ".replace(",", "."),
            price_vnd=price_vnd,
            # Возраст, а не дата: карточка и ранжирование считают его от
            # текущего момента, и фиксированная дата состарила бы каталог сама.
            posted_at=datetime.now(UTC) - timedelta(days=age_days),
        ),
        category=category,
        brand=brand,
        model=model,
        transmission=transmission,
        engine_cc=engine_cc,
        rooms=rooms,
        rental=rental,
    )


def build_catalog() -> tuple[Lot, ...]:
    """Каталог собирается вызовом, потому что возраст лотов отсчитывается от «сейчас»."""
    bike, flat, room, house, cycle = (
        Category.MOTORBIKE,
        Category.APARTMENT,
        Category.ROOM,
        Category.HOUSE,
        Category.BICYCLE,
    )
    return (
        # ── мотобайки ───────────────────────────────────────────────────────
        _lot(
            "honda-lead-2018",
            "Honda Lead 125 2018, автомат, синий",
            category=bike,
            price_vnd=12_000_000,
            age_days=2,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
        # Тот же Lead, но 59 дней от роду: в Нячанге его продали ещё в прошлом
        # месяце, а объявление висит. Ради него в карточке и живёт пометка.
        _lot(
            "honda-lead-old",
            "Honda Lead 125, автомат, документы в порядке",
            category=bike,
            price_vnd=11_500_000,
            age_days=59,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
        # Airblade на запрос Lead: обе Honda, обе автомат, разница только в
        # модели — ровно тот случай, ради которого модель отделяют от марки.
        _lot(
            "honda-airblade-2020",
            "Honda Air Blade 125 2020, автомат",
            category=bike,
            price_vnd=13_500_000,
            age_days=3,
            brand="honda",
            model="airblade",
            transmission="automatic",
            engine_cc=125,
        ),
        _lot(
            "honda-vision-2019",
            "Honda Vision 2019, автомат, один хозяин",
            category=bike,
            price_vnd=9_800_000,
            age_days=5,
            brand="honda",
            model="vision",
            transmission="automatic",
            engine_cc=110,
        ),
        _lot(
            "yamaha-exciter-155",
            "Yamaha Exciter 155 VVA, механика",
            category=bike,
            price_vnd=24_000_000,
            age_days=4,
            brand="yamaha",
            model="exciter",
            transmission="manual",
            engine_cc=155,
        ),
        _lot(
            "yamaha-sirius",
            "Yamaha Sirius, механика, недорого",
            category=bike,
            price_vnd=7_200_000,
            age_days=12,
            source="telegram_groups",
            brand="yamaha",
            model="sirius",
            transmission="manual",
            engine_cc=110,
        ),
        # ── лоты под персон (03.09.2026) ────────────────────────────────────
        #
        # Добавлены не для красоты счётчика: без них персоны, пишущие голое имя
        # модели, не доходили до карточек вовсе, и проверять было нечего —
        # «ничего не нашлось» не отличалось от «выдача мусорная». Каждый лот
        # взят с живой базы Нячанга по формулировке, которая там есть.
        #
        # ADV 150 — самый частый из них: 13 живых лотов из 14 со словом «adv»
        # это ровно «Honda ADV 150 ABS», и цена там 67–80 млн.
        _lot(
            "honda-adv-150",
            "Продам Honda ADV 150 ABS, 2022 год",
            category=bike,
            price_vnd=68_000_000,
            age_days=4,
            source="telegram_groups",
            brand="honda",
            model="adv",
            transmission="automatic",
            engine_cc=150,
        ),
        # Kawasaki Z300 — тот самый лот, из-за которого «z300» разбирали как
        # бюджет. Триста кубов, механика: он же закрывает и «250 кубиков
        # минимум», потому что 300 ≥ 250.
        _lot(
            "kawasaki-z300",
            "Kawasaki Z300 2018, механика, 300 кубов",
            category=bike,
            price_vnd=95_000_000,
            age_days=7,
            brand="kawasaki",
            model="z",
            transmission="manual",
            engine_cc=300,
        ),
        # CBR — семейство без номера в запросе клиента («Honda cbr» из журнала).
        # У лота номер есть, и объём читается из него, как и на стороне клиента.
        _lot(
            "honda-cbr150r",
            "Honda CBR150R, механика, спорт",
            category=bike,
            price_vnd=72_000_000,
            age_days=11,
            brand="honda",
            model="cbr",
            transmission="manual",
            engine_cc=150,
        ),
        _lot(
            "honda-cb200x",
            "Honda CB200X, 200 кубов, механика",
            category=bike,
            price_vnd=46_000_000,
            age_days=6,
            brand="honda",
            model="cb200x",
            transmission="manual",
            engine_cc=200,
        ),
        _lot(
            "vespa-sprint",
            "Vespa Sprint 150 2022, автомат, как новая",
            category=bike,
            price_vnd=62_000_000,
            age_days=9,
            brand="vespa",
            model="sprint",
            transmission="automatic",
            engine_cc=150,
        ),
        _lot(
            "honda-wave-alpha",
            "Honda Wave Alpha, механика, на ходу",
            category=bike,
            price_vnd=6_500_000,
            age_days=21,
            source="telegram_groups",
            brand="honda",
            model="wave",
            transmission="manual",
            engine_cc=110,
        ),
        # ── мотоциклы (côn tay, механика): узнаются по имени семейства ───────
        # Каталог был скутеро-покупочным, а универсальный агент обязан узнавать
        # и мотоциклы: «honda cbr», «kawasaki z300», «yamaha mt» (passport.md,
        # «Марка и модель»). Объём семейства стоит в САМОМ имени и РАЗНЫЙ (CBR650
        # против CBR150), поэтому у модели его нет, а настоящий читается из числа
        # в тексте лота (`engine_size.listing_cc_values`). `engine_cc` правды тут
        # None намеренно: cc несёт текст, а не колонка, — так же, как в
        # `motorbike_models`. Числа в тексте (650/300/890) далеки от 125/200,
        # поэтому на объёмных запросах эти лоты честно отсекаются по объёму.
        _lot(
            "honda-cbr650r",
            "Honda CBR650R 2021, спортбайк, механика",
            category=bike,
            price_vnd=210_000_000,
            age_days=8,
            brand="honda",
            model="cbr",
            transmission="manual",
        ),
        _lot(
            "kawasaki-z300",
            "Kawasaki Z300 2019, нейкед, механика",
            category=bike,
            price_vnd=95_000_000,
            age_days=10,
            brand="kawasaki",
            model="z",
            transmission="manual",
        ),
        _lot(
            "yamaha-mt09",
            "Yamaha MT-09 890cc 2021, механика",
            category=bike,
            price_vnd=250_000_000,
            age_days=12,
            brand="yamaha",
            model="mt",
            transmission="manual",
        ),
        # ── прокат транспорта: оффер аренды, чужая сторона сделки ────────────
        # Клиенту с intent=RENT прокат нужен и показывается; клиенту с intent=BUY
        # он «мимо» и отсекается (`relevance._is_rental_offer`, spec-v2 2.7). Метка
        # оффера — маркер периода в тексте («сутки/месяц», «/сутки»); правда о ней —
        # `rental=True`, ею судит `fit`. Свежие нарочно (age 1): аренда без бюджета
        # и атрибутов — широкий запрос, выдача идёт по свежести, и прокат обязан
        # попасть в пятёрку, иначе «в выдаче есть прокат» проверялось бы вслепую.
        _lot(
            "rental-bikes-shop",
            "Аренда байков Нячанг, сутки/месяц, доставка",
            category=bike,
            price_vnd=4_000_000,
            age_days=1,
            source="telegram_groups",
            rental=True,
        ),
        _lot(
            "rental-scooters-daily",
            "Прокат скутеров, 150.000đ/сутки, шлемы бесплатно",
            category=bike,
            price_vnd=150_000,
            age_days=1,
            source="telegram_groups",
            rental=True,
        ),
        # ── смешанный рынок: чат несёт чужую марку и чужую коробку ───────────
        # Ямаха-скутер как ЧЕСТНАЯ выдача на «ямаха»: без него запрос про марку
        # проверялся бы только отсевом, а «никого не потеряли» — ничем. Доска
        # (chotot), поэтому её структурный фильтр марку и отбирает.
        _lot(
            "yamaha-janus-2021",
            "Yamaha Janus 2021, автомат, скутер",
            category=bike,
            price_vnd=21_000_000,
            age_days=7,
            brand="yamaha",
            model="janus",
            transmission="automatic",
            engine_cc=125,
        ),
        # Kymco из ЧАТА на запрос «ямаха»: realcheck 03.09.2026 — на «ямаха»
        # приходили Honda и Kymco, потому что структурного поля марки у чата нет,
        # а живой отсев марку не читал. Honda-скутер на рынке уже есть доской
        # (Air Blade, Vision выше), а вот Kymco не было нигде — его и добавляем.
        # Объём НЕ задан намеренно: чат его в тексте не пишет, а лот с чужим cc
        # без слова «cc» стал бы «не тот объём» на запрос про объём — дефект,
        # который отсекатель поймать не может, а мерка ловит.
        _lot(
            "kymco-like-chat",
            "Kymco Like скутер, автомат, свежая резина",
            category=bike,
            price_vnd=15_000_000,
            age_days=6,
            source="telegram_groups",
            brand="kymco",
            transmission="automatic",
        ),
        # Механика из чата на запрос «автомат»: та же болезнь, другая ось (на
        # «автомат» приходил Winner). Дешёвая нарочно — чтобы пройти бюджет и
        # упереться в отсев по коробке, а не отсеяться ценой раньше времени и
        # оставить ось коробки непроверенной.
        _lot(
            "yamaha-r15-chat",
            "Yamaha R15 мотоцикл, механика, недорого",
            category=bike,
            price_vnd=13_000_000,
            age_days=5,
            source="telegram_groups",
            brand="yamaha",
            transmission="manual",
        ),
        # Байк, названный ТОЛЬКО моделью, без слова категории: в чате так и пишут.
        # Слов «мотоцикл»/«скутер» здесь нет — категорию лоту даёт ИМЯ МОДЕЛИ
        # (realcheck 03.09.2026: Honda Lead в выдаче студий — тот же класс дефекта).
        # Пока R15 не был моделью, такой лот для запроса о жилье был «неизвестной
        # категорией» и лез в выдачу квартир по свежести; теперь `category_of`
        # выводит из модели motorbike, и отсев чужой категории его убирает. Объём
        # не задан — чат его в тексте не пишет, как у соседних чат-лотов.
        _lot(
            "yamaha-r15-bare",
            "Yamaha R15 2019, механика, идеал",
            category=bike,
            price_vnd=12_000_000,
            age_days=4,
            source="telegram_groups",
            brand="yamaha",
            model="r15",
            transmission="manual",
        ),
        # ── жильё ───────────────────────────────────────────────────────────
        # `rooms` — правда о числе комнат, жёсткий факт (`fit._wrong_rooms`): у
        # студии одна, у «2 спальни» две. Где число комнат в тексте не названо
        # (apt-seaview-lux) — `None`: неизвестное ≠ несовпадение, как у объёма.
        _lot(
            "apt-studio-muong",
            "Студия с мебелью, Мыонг Тхань, длительный срок",
            category=flat,
            price_vnd=8_000_000,
            age_days=2,
            source="telegram_groups",
            rooms=1,
        ),
        # Двушка с мебелью В БЮДЖЕТ. Прежняя стоит 14 млн, и запрос «до 10 млн»
        # отсекал её правильно — значит рынку не хватало не фильтра, а лота: без
        # него персона-жилец получала «ничего не нашлось» на безупречно
        # разобранный паспорт, и проверить выдачу было нечем.
        _lot(
            "flat-two-bed-furnished",
            "2 спальни с мебелью, Винком, длительный срок",
            category=Category.APARTMENT,
            price_vnd=9_000_000,
            age_days=3,
            source="telegram_groups",
        ),
        # Слово «Квартира» в тексте обязательно, иначе категория лота не читается;
        # «стиральной машины» здесь нет намеренно — «машина» ловится паттерном
        # категории CAR (`машин\w*`) и делала лот «чужой категорией» в выдаче жилья.
        _lot(
            "apt-two-rooms",
            "Квартира 2 спальни с мебелью, кондиционер",
            category=flat,
            price_vnd=14_000_000,
            age_days=4,
            source="telegram_groups",
            rooms=2,
        ),
        # Студия у моря — честная выдача на «студию у моря»: число комнат совпадает
        # (1), а вид на море мягко поднимает балл, но не отсекает. Доска (chotot),
        # поэтому число комнат её структурный фильтр и отбирает.
        _lot(
            "apt-studio-seaview",
            "Студия у моря, вид на залив, 6 этаж",
            category=flat,
            price_vnd=9_500_000,
            age_days=4,
            rooms=1,
        ),
        # Квартира БЕЗ мебели на запрос «с мебелью»: мебель — мягкий сигнал, лот
        # остаётся и показывается (число комнат совпадает), балл лишь ниже. Ради
        # этого случая furnished и держат мягким: отсекай по мебели — и половина
        # объявлений, где её просто не упомянули, исчезла бы из выдачи.
        _lot(
            "apt-two-no-furniture",
            "Квартира 2 спальни без мебели, район Лотте",
            category=flat,
            price_vnd=13_000_000,
            age_days=5,
            source="telegram_groups",
            rooms=2,
        ),
        _lot(
            "apt-seaview-lux",
            "Апартаменты с видом на море, бассейн, спортзал",
            category=flat,
            price_vnd=25_000_000,
            age_days=30,
        ),
        _lot(
            "room-shared-kitchen",
            "Комната с общей кухней, район Винком",
            category=room,
            price_vnd=4_500_000,
            age_days=3,
            source="telegram_groups",
        ),
        _lot(
            "house-villa-an-vien",
            "Дом в Ан Виен, 3 спальни, длительный срок",
            category=house,
            price_vnd=45_000_000,
            age_days=8,
            rooms=3,
        ),
        # Чужое число комнат из ЧАТА: структурного поля комнат у чата нет, и «3
        # спальни» на запрос «2 спальни» ловит только текстовый отсев
        # (`relevance._wrong_rooms`), не поле доски. Без такого лота в шуме синтетика
        # этот отсев бы не мерила — та же роль, что у kymco-like-chat для марки.
        # Слово «Квартира» в тексте обязательно: без него категория лота не читается,
        # и на запрос о байке он прошёл бы `_other_category` как «без категории».
        _lot(
            "apt-three-chat",
            "Квартира 3 спальни, свежий ремонт, срочно",
            category=flat,
            price_vnd=12_000_000,
            age_days=6,
            source="telegram_groups",
            rooms=3,
        ),
        # ── чужая категория, которую источник тянет по общему слову ─────────
        _lot(
            "giant-escape-3",
            "Велосипед Giant Escape 3, почти новый",
            category=cycle,
            price_vnd=3_200_000,
            age_days=10,
            source="telegram_groups",
        ),
    )


CATALOG: tuple[Lot, ...] = build_catalog()
