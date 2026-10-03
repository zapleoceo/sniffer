"""Подготовка текста к чтению фактов: что убирается и почему.

Каждый случай — реальный шаблон из чатов (разбор 18 868 активных карточек, 03.10.2026),
сокращённый до строк, на которых он держится.
"""

from __future__ import annotations

from sniffer.domain.facts_text import MAX_LINE, MAX_TEXT, fact_text, fold


def test_vietnamese_diacritics_fold_to_plain_letters_and_cyrillic_stays() -> None:
    """«Phước Hải» и «Phuoc Hai» — одно место, а «й» не должна стать «и»."""
    assert fold("Phước Hải — Đà Nẵng") == "phuoc hai — da nang"
    assert fold("đường Đồng") == "duong dong"
    assert fold("Ёлка, майка") == "елка, майка"


def test_the_agency_menu_is_not_part_of_the_listing() -> None:
    """Меню AN-HOME стоит в подвале 1437 карточек и дарило каждой «студию» и «Океанус»."""
    text = (
        "Дом с 3 спальнями в My Gia\n"
        "Контракт: 12 месяцев\n"
        "──────\n"
        "👉 ДОМА И ВИЛЛЫ\n"
        "👉 КВАРТИРЫ И СТУДИИ\n"
        "👉 АРЕНДА В ЖК ОКЕАНУС\n"
        "👉 АРЕНДА С ЖИВОТНЫМИ\n"
        "👉 АРЕНДА ДО 10 МЛН\n"
    )

    prepared = fact_text(text)

    assert "студи" not in prepared.folded
    assert "океанус" not in prepared.folded
    assert "живот" not in prepared.folded
    assert "my gia" in prepared.folded


def test_a_headline_that_merely_starts_like_a_menu_item_is_kept() -> None:
    """Меню — строка целиком: «Аренда квартир у моря» — заголовок, и факты из него читаются."""
    assert "у моря" in fact_text("Аренда квартир у моря, 35 м2").folded


def test_the_danang_menu_does_not_turn_a_nha_trang_post_into_a_danang_one() -> None:
    text = (
        "Квартира в Фыок Хай\n📱 HOUSE IN DANANG 🖥\n📱 APARTMENT IN DANANG 🖥\n📱 VILLA IN DANANG 🖥"
    )

    assert "danang" not in fact_text(text).folded


def test_hashtags_and_links_carry_no_facts() -> None:
    """«#oceanus» — тег агентства, а не район; ссылка — не слова."""
    prepared = fact_text("Студия 30 м2 #oceanus #нячанг\nhttps://example.com/x\nt.me/some_channel")

    assert "oceanus" not in prepared.folded
    assert "example" not in prepared.folded
    assert "some_channel" not in prepared.folded
    assert "студия 30 м2" in prepared.folded


def test_emoji_digits_are_read_as_digits_before_anything_else() -> None:
    """Цену и площадь прячут цифрами-эмодзи; общая очистка (`clean_text`) их читает."""
    assert "площадь: 45 м2" in fact_text("Площадь: 4️⃣5️⃣ м²").folded


def test_the_two_views_differ_only_in_diacritics() -> None:
    """«tặng 1 tháng» (подарок) без диакритики — «tang 1 thang», то есть «этаж 1»."""
    prepared = fact_text("Tặng 1 tháng, tầng 3")

    assert "tặng" in prepared.low
    assert "tang 1 thang, tang 3" in prepared.folded


def test_the_text_and_every_line_are_capped() -> None:
    """Воронка берёт сообщения по одному: простыня не вправе её остановить."""
    long_line = "а" * (MAX_LINE * 3)
    huge = "б" * (MAX_TEXT * 2)

    assert all(len(line) <= MAX_LINE for line in fact_text(long_line).lines)
    assert len(fact_text(huge).folded) <= MAX_TEXT
