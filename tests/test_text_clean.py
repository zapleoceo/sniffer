"""Очистка текста: цифры, написанные не цифрами, снова становятся цифрами."""

from __future__ import annotations

import pytest

from sniffer.domain.text_clean import clean_text


@pytest.mark.parametrize(
    ("written", "cleaned"),
    [
        # «жирные» математические цифры и буквы: живой шаблон «#нячанг #аренда #сдам»
        ("𝟙𝟚 𝟝𝟘𝟘 𝟘𝟘𝟘 𝕧𝕟𝕕", "12 500 000 vnd"),
        # цифры-эмодзи («кейкапы») со значком вместо разделителя тысяч
        ("1️⃣5️⃣🔣0️⃣0️⃣0️⃣🔣0️⃣0️⃣0️⃣/мес.", "15 000 000/мес."),
        ("7️⃣⚪️5️⃣0️⃣0️⃣⚪️0️⃣0️⃣0️⃣VND / мес", "7 500 000VND / мес"),
        # кириллическая «О» вместо нуля внутри числа
        ("5ОО.ООО vnd /день", "500.000 vnd /день"),
        # квадратные скобки вокруг цены мешают единице найти число
        ("Цена: [ 53.000.000 ] VND", "Цена:   53.000.000   VND"),
        # невидимые знаки, неразрывный пробел
        ("Цена:​ 9 000 000", "Цена: 9 000 000"),
    ],
)
def test_digits_written_as_something_else_become_digits(written: str, cleaned: str) -> None:
    assert clean_text(written) == cleaned


@pytest.mark.parametrize(
    "text",
    [
        "Oct 2 O 10 Ого",  # «O» не внутри числа остаётся буквой
        "Honda Vision 2018, 125cc",
        "тел +84 905 123 456",  # телефон остаётся телефоном
        "Вилла в центре, 3 спальни",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert clean_text(text) == text
