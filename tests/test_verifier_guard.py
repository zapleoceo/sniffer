"""Охранник выдачи: что он вправе сделать с карточкой, а что нет."""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.broker.client import BrokerError
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.sources.base import RawItem
from sniffer.verifier.guard import screen

NO_BUDGET = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
WANTED = Passport(
    intent=Intent.BUY,
    category=Category.MOTORBIKE,
    city="nha_trang",
    budget=Budget(max=10_000_000, currency=Currency.VND),
)


def item(number: int, text: str, **overrides: Any) -> RawItem:
    fields: dict[str, Any] = {
        "source": "telegram_groups",
        "external_id": str(number),
        "url": f"https://t.me/c/1/{number}",
        "text": text,
    }
    fields.update(overrides)
    return RawItem(**fields)


class FakeBroker:
    """Брокер без сети. Отдаёт заготовленный вердикт или падает."""

    def __init__(
        self, payload: dict[str, Any] | None = None, boom: Exception | None = None
    ) -> None:
        self.payload = payload or {"verdicts": []}
        self.boom = boom
        self.calls = 0
        self.prompts: list[str] = []
        self.options: list[dict[str, Any]] = []

    async def structured(self, prompt: str, **options: Any) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        self.options.append(options)
        if self.boom is not None:
            raise self.boom
        return self.payload

    async def aclose(self) -> None:
        return None


def verdict(number: int, **fields: Any) -> dict[str, Any]:
    row = {
        "n": number,
        "keep": True,
        "why": "",
        "subject": "motorbike",
        "price_vnd": "",
        "price_text": "",
    }
    row.update(fields)
    return row


async def test_a_rejected_card_never_reaches_the_client() -> None:
    """Ради этого охранник и заведён: колонка JBL в ответ на «мотоцикл»."""
    items = [item(1, "Продам Honda Vision"), item(2, "Loa Bluetooth JBL Charge 5")]
    broker = FakeBroker({"verdicts": [verdict(1), verdict(2, keep=False, why="это колонка")]})

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert [card.external_id for card in kept] == ["1"]


async def test_guard_uses_sales_lane_without_losing_structured_contract() -> None:
    broker = FakeBroker()
    await screen(WANTED, [item(1, "Honda Vision")], broker=broker)  # type: ignore[arg-type]
    assert broker.calls == 1
    options = broker.options[0]
    assert options["capability"] == "chat:sales"
    assert options["schema_name"] == "listing_guard"
    assert options["schema"]["additionalProperties"] is False
    assert options["schema"]["required"] == ["verdicts"]


async def test_a_price_written_without_a_label_is_recovered() -> None:
    """«Штормовая скидка! 40 миллионов» — регексп такое не берёт, модель берёт."""
    items = [item(1, "Штормовая скидка! 40 миллионов) Кастомный байк")]
    broker = FakeBroker({"verdicts": [verdict(1, price_vnd="40000000", price_text="40 миллионов")]})

    # Паспорт без бюджета: тест про добычу цены, а не про сравнение её с ним.
    (card,) = await screen(NO_BUDGET, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd == 40_000_000
    assert card.price_raw == "40 миллионов", "клиенту показываем слова продавца"


async def test_an_invented_price_is_thrown_away_not_shown() -> None:
    """Придуманная цена не смеет стать утверждением перед клиентом.

    Фрагмент, которого в объявлении нет, — выдумка модели. Карточку оставляем:
    она могла быть верной и без цены; цену — выбрасываем целиком.
    """
    items = [item(1, "Продам байк, звоните")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="5000000", price_text="Цена: 5.000.000 VND")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd is None and card.price_raw == ""


async def test_our_own_price_is_never_overwritten() -> None:
    """Регексп взял цену по явной метке — это надёжнее догадки модели."""
    items = [item(1, "Цена: 7.000.000 VND", price_vnd=7_000_000, price_raw="Цена: 7.000.000 VND")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="9000000", price_text="Цена: 7.000.000 VND")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd == 7_000_000


async def test_an_implausible_number_is_not_a_price() -> None:
    """Десять миллиардов донгов — это ошибка чтения, а не байк в Нячанге."""
    items = [item(1, "Продам байк 99999999999999")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="99999999999999", price_text="99999999999999")]}
    )

    (card,) = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert card.price_vnd is None


async def test_a_card_the_model_said_nothing_about_survives() -> None:
    """Молчание — не отказ: непроверенная карточка лучше потерянной."""
    items = [item(1, "Продам байк"), item(2, "Продам скутер")]
    broker = FakeBroker({"verdicts": [verdict(1)]})

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert [card.external_id for card in kept] == ["1", "2"]


@pytest.mark.parametrize("boom", [BrokerError("брокер лёг"), TimeoutError(), OSError("сеть")])
async def test_a_broken_guard_does_not_cancel_the_answer(boom: Exception) -> None:
    """Охранник — улучшение, а не условие работы."""
    items = [item(1, "Продам байк"), item(2, "Продам скутер")]
    broker = FakeBroker(boom=boom)

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert len(kept) == 2


async def test_only_checked_cards_reach_the_client() -> None:
    """Непроверенный хвост не показывается — и это исправление, а не экономия.

    Сначала хвост ехал следом за проверенным: «о нём ничего не известно,
    выбрасывать не за что». Замер 01.09.2026 показал цену такой логики: охранник
    снял десять карточек из двенадцати, наверх всплыл хвост, куда ранжирование
    сложило заведомо худшее, и на «мотоцикл до 300 USD» клиент увидел ноутбуки
    ThinkPad — не потому что охранник их пропустил, а потому что до них не дошёл.
    """
    items = [item(number, "Продам байк") for number in range(1, 31)]
    broker = FakeBroker({"verdicts": []})

    kept = await screen(WANTED, items, broker=broker, limit=5)  # type: ignore[arg-type]

    assert broker.calls == 1, "платим за пятерых, а не за тридцать"
    assert len(kept) == 5, "показываем только то, что проверили"
    assert broker.prompts[0].count(") Продам байк") == 5


async def test_nothing_to_check_costs_nothing() -> None:
    broker = FakeBroker()

    assert await screen(WANTED, [], broker=broker) == []  # type: ignore[arg-type]
    assert broker.calls == 0


async def test_the_budget_reaches_the_model_in_dongs() -> None:
    """Объявления написаны в донгах, а бюджет клиент назвал в долларах."""
    dollars = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=300, currency=Currency.USD),
    )
    broker = FakeBroker({"verdicts": []})

    await screen(dollars, [item(1, "байк")], broker=broker, usd_vnd=26000)  # type: ignore[arg-type]

    assert "7 800 000 VND" in broker.prompts[0]


async def test_without_a_rate_the_model_is_not_told_a_made_up_budget() -> None:
    dollars = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=300, currency=Currency.USD),
    )
    broker = FakeBroker({"verdicts": []})

    await screen(dollars, [item(1, "байк")], broker=broker)  # type: ignore[arg-type]

    # Слово «бюджет» есть в самой инструкции — проверяем именно потолок.
    assert "бюджет до" not in broker.prompts[0]


async def test_the_client_request_is_stated_in_human_words() -> None:
    """Со строкой `motorbike` модель отбраковывала скутеры.

    Замер 01.09.2026, дословная причина отказа: «Это мопед/скутер электрический,
    а клиент ищет motorbike». Модель читала имя поля как требование, потому что
    человек так не пишет. Запрос клиента описывается словами; список значений
    для ответа — отдельно, и там имена полей уместны.
    """
    broker = FakeBroker({"verdicts": []})

    await screen(WANTED, [item(1, "байк")], broker=broker)  # type: ignore[arg-type]

    prompt = broker.prompts[0]
    lines = prompt.split("\n")
    wanted_line = next(line for line in lines if line.startswith("Клиент ищет"))
    assert "скутер" in wanted_line and "мотобайк" in wanted_line
    assert "motorbike" not in wanted_line
    assert "Нячанг" in wanted_line and "nha_trang" not in wanted_line


async def test_the_model_is_asked_what_the_ad_is_about_not_whether_it_fits() -> None:
    """Вопрос «что это» она отвечает на любом языке; «подходит ли» — нет.

    Замер 01.09.2026: на пяти вьетнамских объявлениях о домах и земле модель
    ТРИ РАЗА ИЗ ТРЁХ ответила «подходит» для запроса про мотоцикл. Правила
    отказа были по-русски, объявление по-вьетнамски.
    """
    broker = FakeBroker({"verdicts": []})

    await screen(WANTED, [item(1, "Bán nhà 2 tầng")], broker=broker)  # type: ignore[arg-type]

    prompt = broker.prompts[0]
    assert "ЧТО в нём продают" in prompt
    assert "вьетнамском" in prompt, "модель обязана знать, что текст может быть не по-русски"


async def test_an_ad_about_another_subject_is_dropped_by_code() -> None:
    """Сравнение двух строк — работа кода, а не модели."""
    items = [item(1, "Bán nhà 2 tầng hẻm oto"), item(2, "SYM Attila 50cc")]
    broker = FakeBroker(
        {
            "verdicts": [
                verdict(1, subject="house", keep=True),
                verdict(2, subject="motorbike", keep=True),
            ]
        }
    )

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert [card.external_id for card in kept] == ["2"]


async def test_an_unrecognised_subject_is_left_to_the_client() -> None:
    """Непонятное объявление решает человек, а не мы за него."""
    broker = FakeBroker({"verdicts": [verdict(1, subject="unknown")]})

    kept = await screen(WANTED, [item(1, "??? срочно")], broker=broker)  # type: ignore[arg-type]

    assert len(kept) == 1


async def test_a_missing_price_is_never_a_reason_to_refuse() -> None:
    """Половина объявлений Нячанга цену не пишет вовсе.

    Замер 01.09.2026: охранник снял десять карточек из двенадцати с причиной
    «Цена в тексте отсутствует» — то есть выбросил ровно то, ради чего заведён.
    Правило теперь стоит в промпте явным пунктом.
    """
    broker = FakeBroker({"verdicts": []})

    await screen(WANTED, [item(1, "байк")], broker=broker)  # type: ignore[arg-type]

    assert "отсутствие цены НЕ причина отказа" in broker.prompts[0]


# ── бюджет считает код, а не модель ─────────────────────────────────────────


async def test_a_listing_far_over_budget_is_dropped_by_arithmetic() -> None:
    """Ровно тот случай, с которого всё началось: 100 млн VND на бюджет 300 USD."""
    items = [item(1, "Honda CB400 Custom. Цена: 100 000 000 VND")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="100000000", price_text="Цена: 100 000 000 VND")]}
    )

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert kept == []


async def test_slightly_over_budget_still_reaches_the_client() -> None:
    """Чуть дороже клиент вправе увидеть и решить сам — как и в ранжировании."""
    items = [item(1, "Байк. Цена: 11 000 000 VND")]
    broker = FakeBroker(
        {"verdicts": [verdict(1, price_vnd="11000000", price_text="Цена: 11 000 000 VND")]}
    )

    kept = await screen(WANTED, items, broker=broker)  # type: ignore[arg-type]

    assert len(kept) == 1, "бюджет 10 млн, запас 30% — 11 млн проходит"


async def test_a_listing_without_a_price_is_not_called_expensive() -> None:
    """«Цену не написали» не значит «дорого»."""
    items = [item(1, "Продам байк, звоните")]
    broker = FakeBroker({"verdicts": [verdict(1)]})

    assert len(await screen(WANTED, items, broker=broker)) == 1  # type: ignore[arg-type]


async def test_the_model_is_not_asked_to_compare_prices() -> None:
    """Арифметику у модели забрали — и промпт обязан это говорить.

    Замер 01.09.2026: на «до 300 USD» она оставила байки за 10–11 млн при
    потолке 7.8, а на «до 400 USD» отбраковала всё при потолке 10.4. Один
    промпт, соседние запросы, противоположная строгость.
    """
    broker = FakeBroker({"verdicts": []})

    await screen(WANTED, [item(1, "байк")], broker=broker)  # type: ignore[arg-type]

    assert "цену с бюджетом НЕ сравнивай" in broker.prompts[0]
