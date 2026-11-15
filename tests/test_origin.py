"""Первоисточник сообщения: пересылка, указатель, каноническая ссылка (domain/origin.py)."""

from __future__ import annotations

from types import SimpleNamespace as Ns

import pytest

from sniffer.domain.origin import (
    OWN_POST,
    OriginKind,
    PostRef,
    canonical_telegram_url,
    forward_origin,
    message_origin,
    pointer_origin,
    same_post_key,
)


def _forward(**header: object) -> Ns:
    defaults: dict[str, object] = {
        "from_id": None,
        "channel_post": None,
        "saved_from_peer": None,
        "saved_from_msg_id": None,
    }
    return Ns(fwd_from=Ns(**{**defaults, **header}), forward=None)


def test_an_own_message_is_its_own_origin() -> None:
    assert forward_origin(Ns(fwd_from=None)) is OWN_POST
    assert OWN_POST.primary


def test_a_channel_forward_points_to_the_channel_post() -> None:
    message = _forward(from_id=Ns(channel_id=1234567890), channel_post=55)
    message.forward = Ns(chat=Ns(username="ArendaChannel"))

    origin = forward_origin(message)

    assert origin.kind is OriginKind.FORWARD
    assert origin.post == PostRef(55, -1001234567890, "ArendaChannel")
    assert origin.url == "https://t.me/ArendaChannel/55"
    assert origin.primary


def test_a_saved_peer_is_used_when_the_channel_post_is_absent() -> None:
    message = _forward(saved_from_peer=Ns(channel_id=42), saved_from_msg_id=7)

    origin = forward_origin(message)

    assert origin.post == PostRef(7, -1000000000042, None)
    # Адрес известен, но имени нет — публичной ссылки нет, первоисточник не выдаём.
    assert origin.url is None and not origin.primary


@pytest.mark.parametrize(
    "header",
    [
        {"from_id": Ns(user_id=7)},  # переслано от человека — поста нет
        {"from_id": None},  # скрытый автор (`from_name`)
        {"from_id": Ns(channel_id=5), "channel_post": None},  # канал без номера поста
    ],
)
def test_a_forward_without_a_post_address_is_not_primary(header: dict[str, object]) -> None:
    origin = forward_origin(_forward(**header))

    assert origin.kind is OriginKind.FORWARD_HIDDEN
    assert not origin.primary


def test_a_mocked_username_is_not_trusted_as_a_chat_name() -> None:
    message = _forward(from_id=Ns(channel_id=9), channel_post=3)
    message.forward = Ns(chat=Ns(username="has space"))

    assert forward_origin(message).post == PostRef(3, -1000000000009, None)


@pytest.mark.parametrize(
    ("text", "post"),
    [
        ("👉 https://t.me/arenda_v_nyachang/12345", PostRef(12345, username="arenda_v_nyachang")),
        (
            "Смотрите тут t.me/s/Arenda_Nyachangg/99?single",
            PostRef(99, username="Arenda_Nyachangg"),
        ),
        ("актуально https://t.me/c/1234567890/77", PostRef(77, chat_tg_id=-1001234567890)),
        ("bit.ly/3abcd", None),
        ("Подробнее: https://example.com/flat/1", None),
    ],
)
def test_a_link_with_almost_nothing_around_is_a_pointer(text: str, post: PostRef | None) -> None:
    origin = pointer_origin(text)

    assert origin is not None and origin.kind is OriginKind.POINTER
    assert origin.post == post
    assert not origin.primary


@pytest.mark.parametrize(
    "text",
    [
        "Сдам студию 6 млн t.me/rent_owner",  # ссылка на автора — контакт, не указатель
        "Сдам дом 10 млн https://maps.app.goo.gl/xyz",  # карта — место объекта
        "Cho thuê 8.500.000đ/tháng",  # цена с точками — не адрес
        "Пишите @rent_owner, 6 млн",
        (
            "Сдаётся студия в Нячанге, район Тхап Ба, 30 м2, вид на горы, 6 млн в месяц. "
            "Подробности https://t.me/arenda_v_nyachang/12345"
        ),
    ],
)
def test_a_real_advert_with_a_link_is_not_a_pointer(text: str) -> None:
    assert pointer_origin(text) is None


def test_a_forward_wins_over_the_text_of_the_message() -> None:
    message = _forward(from_id=Ns(channel_id=1), channel_post=2)

    assert message_origin(message, "https://t.me/other/1").kind is OriginKind.FORWARD
    assert message_origin(Ns(fwd_from=None), "https://t.me/other/1").kind is OriginKind.POINTER
    assert message_origin(Ns(), "Продам Honda Vision 2021, 15 млн") is OWN_POST


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("http://telegram.me/s/Foo_bar/12?single", "https://t.me/Foo_bar/12"),
        ("https://www.t.me/Foo_bar/12/", "https://t.me/Foo_bar/12"),
        ("https://t.me/forum_chat/5/123", "https://t.me/forum_chat/5/123"),
        ("https://t.me/c/123/45/", "https://t.me/c/123/45"),
        ("t.me/user", None),
        ("https://chotot.com/x/1", None),
        ("https://t.me/c/123", None),
    ],
)
def test_one_post_has_one_canonical_link(url: str, canonical: str | None) -> None:
    assert canonical_telegram_url(url) == canonical


def test_link_comparison_ignores_the_case_of_the_chat_name() -> None:
    """Регистр имени Telegram безразличен, но ссылку ради него не переписываем."""
    assert canonical_telegram_url("https://t.me/Arenda_Nyachangg/1") == (
        "https://t.me/Arenda_Nyachangg/1"
    )
    assert same_post_key("https://t.me/Arenda_Nyachangg/1") == same_post_key(
        "http://telegram.me/arenda_nyachangg/1"
    )
