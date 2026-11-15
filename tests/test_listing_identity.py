"""Ключи «то же объявление»: отпечаток без цены и ключи наблюдения каталога."""

from __future__ import annotations

from sniffer.domain.listing_identity import identity_hash, identity_keys

FLAT = (
    "Сдаётся студия в Нячанге, район Тхап Ба, 30 м2, вид на горы, мебель, кондиционер. "
    "Цена 6 млн/мес. Тел 0901234567"
)


def test_a_repost_with_a_new_price_is_the_same_advert() -> None:
    assert identity_hash(FLAT) == identity_hash(FLAT.replace("6 млн", "5,5 млн"))


def test_emoji_and_spacing_do_not_make_a_new_advert() -> None:
    assert identity_hash(FLAT) == identity_hash("🔥 " + FLAT.replace(", ", ",  ") + " 🔥")


def test_other_numbers_still_tell_adverts_apart() -> None:
    """Площадь, телефон и кубатура — часть объявления, а не цена."""
    assert identity_hash(FLAT) != identity_hash(FLAT.replace("30 м2", "45 м2"))
    assert identity_hash(FLAT) != identity_hash(FLAT.replace("0901234567", "0907654321"))


def test_a_short_text_gets_no_identity() -> None:
    assert identity_hash("Сдам студию, 6 млн") is None


def test_copies_of_one_forward_share_the_link_key() -> None:
    first = identity_keys("https://t.me/Channel/5", "текст один", textual=True)
    second = identity_keys("http://telegram.me/s/channel/5", "текст два", textual=True)

    assert first[0] == second[0] == "url:https://t.me/channel/5"


def test_chotot_gets_only_the_link_key() -> None:
    """У Chotot в тексте JSON с номером лота: текстовые ключи там ничего не ловят."""
    keys = identity_keys("https://www.chotot.com/xe/1.htm", FLAT, textual=False)

    assert keys == ("url:https://www.chotot.com/xe/1.htm",)


def test_telegram_gets_text_and_near_keys() -> None:
    keys = identity_keys("https://t.me/a/1", FLAT, textual=True)

    assert [key.split(":", 1)[0] for key in keys] == ["url", "text", "near"]
