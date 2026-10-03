"""Меню поисков и команда `/new` на стороне хендлера: управление, легенда, текст.

Хендлер тонкий, поэтому здесь проверяются ровно его решения: чьим поиском можно
управлять, что обещает список и как команда достаёт текст. Устройство самого
поиска (хранилище, порядок, вытеснение) — в соседних файлах.

Команду гоняем через НАСТОЯЩИЙ фильтр aiogram и настоящий `Message`: прежняя
версия резала `message.text` по пробелу сама и тихо расходилась с фильтром на
переводе строки и на подписи к фото.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Chat, Message, User

from sniffer.bot import billing, keyboards, query_menu, threads
from sniffer.bot.handlers import search as handler
from sniffer.bot.keyboards import RequestsCallback, requests_markup
from sniffer.bot.store import Client
from sniffer.domain.passport import Budget, Category, Currency, Intent
from sniffer.domain.records import QueryOverview
from sniffer.domain.threads import MAX_LIVE_THREADS
from tests.test_bot_dialog import FakeCallback, FakeMessage, FakeUser
from tests.thread_support import CLIENT, bike

# Разметка, которую Bot API принимает в режиме HTML: теги и сущности. Всё прочее
# «<», «>» и «&» он отвергает целиком — «can't parse entities».
_ALLOWED_TAG = re.compile(r"</?(?:b|i|u|s|code|pre)>|<a href=\"[^\"<>]*\">|</a>")
_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")


def assert_telegram_html(text: str) -> None:
    """Сообщение, которое Telegram не отвергнет: посторонних «<», «>» и голого «&» нет."""
    bare = _ENTITY.sub("", _ALLOWED_TAG.sub("", text))
    assert "<" not in bare and ">" not in bare and "&" not in bare, f"Telegram отвергнет: {text!r}"


# ── /new: текст команды берёт фильтр ────────────────────────────────────────


def _command_message(*, text: str | None = None, caption: str | None = None) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Дима", username="dima"),
        text=text,
        caption=caption,
    )


async def _run_new(
    monkeypatch: pytest.MonkeyPatch, *, text: str | None = None, caption: str | None = None
) -> tuple[list[int], list[str], list[str]]:
    """Команда от настоящего фильтра до хендлера. Возвращает: взведено, искали, ответили."""
    armed: list[int] = []
    searched: list[str] = []
    answers: list[str] = []

    class Talker:
        async def start_new(self, client: Client) -> None:
            armed.append(client.tg_user_id)

        async def on_text(self, _client: Client, query: str, _send: Any) -> None:
            searched.append(query)

    async def answer(_self: Message, reply: str, **_kwargs: object) -> None:
        answers.append(reply)

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(handler, "conversation", lambda: Talker())
    message = _command_message(text=text, caption=caption)
    parsed = await Command("new")(message, bot=cast(Bot, None))
    assert isinstance(parsed, dict), "фильтр aiogram не принял команду"
    await handler.new_request(message, **parsed)
    return armed, searched, answers


async def test_bare_new_arms_the_thread_and_asks_what_to_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed, searched, answers = await _run_new(monkeypatch, text="/new")

    assert armed == [42]
    assert searched == []
    assert answers == [threads.ASK_WHAT]


@pytest.mark.parametrize(
    ("text", "caption", "expected"),
    [
        ("/new ищу скутер в Нячанге", None, "ищу скутер в Нячанге"),
        ("/new\nквартира в Нячанге до 10 млн", None, "квартира в Нячанге до 10 млн"),
        ("/new\nквартира", None, "квартира"),
        ("/new   сниму байк  ", None, "сниму байк"),
        (None, "/new скутер", "скутер"),
        (None, "/new\nскутер до 400", "скутер до 400"),
    ],
    ids=["space", "newline_long", "newline_short", "padded", "photo_caption", "caption_newline"],
)
async def test_new_with_words_searches_exactly_what_the_filter_extracted(
    monkeypatch: pytest.MonkeyPatch, text: str | None, caption: str | None, expected: str
) -> None:
    """Перевод строки после команды и подпись к фото не теряют текст.

    Прежний разбор `message.text.partition(" ")` на «/new⏎квартира» отдавал в поиск
    «в Нячанге до 10 млн» без предмета (и «квартира» без пробела не искала ничего),
    а подпись к фото, у которой `message.text` пуст, не искала вовсе.
    """
    armed, searched, answers = await _run_new(monkeypatch, text=text, caption=caption)

    assert armed == [42], "взводится и с текстом: у «начни новый поиск» один вход"
    assert searched == [expected]
    assert answers == [], "со словами спрашивать нечего"


# ── управление поиском — по принадлежности, а не по списку ──────────────────


def _managed(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], FakeMessage, Any]:
    """Меню с двумя поисками в списке и одним, вытесненным из него (корень 99)."""
    visible = QueryOverview(root=1, passport=bike(), monitoring="off", is_active=True)
    pushed = QueryOverview(
        root=99, passport=bike(raw_query="старый поиск", city="da_nang"), monitoring="active"
    )
    owned = {1: visible, 99: pushed}
    calls: list[Any] = []

    async def list_for(_client: Client) -> query_menu.Menu:
        raise AssertionError("управление поиском не должно зависеть от списка из пяти")

    async def get_one(_client: Client, root: int) -> QueryOverview | None:
        return owned.get(root)

    async def select(_client: Client, root: int, *, editing: bool = False) -> bool:
        calls.append(("select", root, editing))
        return True

    async def toggle(_client: Client, root: int, *, active: bool) -> bool:
        calls.append(("toggle", root, active))
        return True

    class Talker:
        async def repeat(self, _client: Client, root: int, _send: Any) -> None:
            calls.append(("repeat", root))

    monkeypatch.setattr(handler, "Message", FakeMessage)
    monkeypatch.setattr(query_menu, "list_for", list_for)
    monkeypatch.setattr(query_menu, "get_one", get_one)
    monkeypatch.setattr(query_menu, "select", select)
    monkeypatch.setattr(query_menu, "toggle", toggle)
    monkeypatch.setattr(handler, "conversation", lambda: Talker())
    message = FakeMessage("", from_user=FakeUser())
    return calls, message, cast(Any, FakeCallback(message))


async def test_every_action_works_on_a_search_that_is_not_in_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вытесненный из списка поиск остаётся поиском клиента — и им можно управлять.

    До правки `manage_request` искал корень в списке из пяти и на вытесненном
    отвечал «не найден» на всё: паузу, «Искать снова», «Изменить». Мониторинг при
    этом идёт и платится независимо от списка, а кнопка «выключить» не работала.
    """
    calls, message, callback = _managed(monkeypatch)

    for action in ("open", "search", "edit", "pause", "resume"):
        await handler.manage_request(callback, RequestsCallback(action=action, root=99))

    texts = [text for text, _keyboard in message.answers]
    assert not any(threads.NOT_FOUND in text for text in texts), texts
    assert ("select", 99, False) in calls, "открыть"
    assert ("repeat", 99) in calls, "искать снова"
    assert ("select", 99, True) in calls, "изменить"
    assert ("toggle", 99, False) in calls, "пауза"
    assert ("toggle", 99, True) in calls, "возобновить"


async def test_an_unknown_or_foreign_search_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Принадлежность по-прежнему проверяется: чужой корень — «не найден», и ничего не меняется."""
    calls, message, callback = _managed(monkeypatch)

    for action in ("open", "search", "edit", "pause", "resume"):
        await handler.manage_request(callback, RequestsCallback(action=action, root=12345))

    assert calls == []
    assert [text for text, _keyboard in message.answers] == [threads.NOT_FOUND] * 5


# ── легенда списка ──────────────────────────────────────────────────────────


async def _list_text_and_labels(
    monkeypatch: pytest.MonkeyPatch, menu: query_menu.Menu
) -> tuple[str, list[str]]:
    async def list_for(_client: Client) -> query_menu.Menu:
        return menu

    monkeypatch.setattr(handler, "Message", FakeMessage)
    monkeypatch.setattr(query_menu, "list_for", list_for)
    message = FakeMessage("", from_user=FakeUser())

    await handler.requests(cast(Message, message))

    text, keyboard = message.answers[0]
    return text, [button.text for row in keyboard.inline_keyboard for button in row]


def _two_searches() -> list[QueryOverview]:
    return [
        QueryOverview(root=1, passport=bike(), is_active=True),
        QueryOverview(root=2, passport=bike(category=Category.APARTMENT, intent=Intent.RENT)),
    ]


async def test_the_check_mark_means_the_next_message_refines_that_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, labels = await _list_text_and_labels(monkeypatch, query_menu.Menu(items=_two_searches()))

    assert threads.LIST_HEADER in text
    assert any(label.startswith("✓ ") for label in labels)


async def test_while_new_is_armed_the_list_does_not_claim_a_current_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока взведён `/new`, «✓» врёт: следующее сообщение откроет НОВЫЙ поиск.

    Живая проверка из разбора: `/new`, `/requests` — галочка на «Скутер, Нячанг»,
    а следующее «до 500» открыло новую ветку.
    """
    text, labels = await _list_text_and_labels(
        monkeypatch, query_menu.Menu(items=_two_searches(), starting_new=True)
    )

    assert threads.LIST_HEADER_NEW in text
    assert threads.LIST_HEADER not in text
    assert not any("✓" in label for label in labels)


def test_the_marker_can_be_switched_off_in_the_keyboard_itself() -> None:
    keyboard = requests_markup(_two_searches(), marked=False)

    assert not any("✓" in button.text for row in keyboard.inline_keyboard for button in row)


async def test_twin_searches_get_distinct_buttons_in_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    twins = [
        QueryOverview(
            root=1, passport=bike(budget=Budget(max=300, currency=Currency.USD)), is_active=True
        ),
        QueryOverview(root=2, passport=bike(budget=Budget(max=1000, currency=Currency.USD))),
    ]

    _text, labels = await _list_text_and_labels(monkeypatch, query_menu.Menu(items=twins))

    names = labels[:2]
    assert len(set(names)) == 2, f"две одинаковые кнопки: {names}"
    assert "до 300 USD" in names[0]
    assert "до 1 000 USD" in names[1]


async def test_an_empty_list_says_there_are_no_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(_client: Client) -> query_menu.Menu:
        return query_menu.Menu(items=[])

    monkeypatch.setattr(handler, "Message", FakeMessage)
    monkeypatch.setattr(query_menu, "list_for", nothing)
    message = FakeMessage("", from_user=FakeUser())

    await handler.requests(cast(Message, message))

    assert message.answers[0][0] == threads.NO_SEARCHES


# ── сообщения хендлера безопасны для HTML ───────────────────────────────────


async def test_the_card_and_the_edit_prompt_survive_markup_in_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Карточка поиска и «Изменяем» — те места, где название идёт в сообщение с HTML."""
    angry = QueryOverview(
        root=7, passport=bike(category=None, raw_query="что-нибудь <300$ & <i>"), monitoring="off"
    )

    async def get_one(_client: Client, _root: int) -> QueryOverview:
        return angry

    async def select(_client: Client, _root: int, *, editing: bool = False) -> bool:
        return True

    monkeypatch.setattr(handler, "Message", FakeMessage)
    monkeypatch.setattr(query_menu, "get_one", get_one)
    monkeypatch.setattr(query_menu, "select", select)
    message = FakeMessage("", from_user=FakeUser())
    callback = cast(Any, FakeCallback(message))

    await handler.manage_request(callback, RequestsCallback(action="open", root=7))
    await handler.manage_request(callback, RequestsCallback(action="edit", root=7))

    assert len(message.answers) == 2
    for text, _keyboard in message.answers:
        assert_telegram_html(text)
        assert "&lt;300$ &amp; &lt;i&gt;" in text


def test_the_html_checker_itself_rejects_what_telegram_rejects() -> None:
    """Проверяющий не должен быть всеядным: иначе тесты выше ничего не доказывают."""
    assert_telegram_html("<b>Мотобайк</b> &lt;300")
    for bad in ("Что-нибудь <300$", "a & b", "<b>x</b> <script>", "5 > 3"):
        with pytest.raises(AssertionError):
            assert_telegram_html(bad)


# ── слово «поиски» ──────────────────────────────────────────────────────────


def test_the_person_sees_searches_not_branches_or_requests() -> None:
    """Одно слово на одну вещь: «поиски». «Ветка» — внутренний термин, а «запросы» — третье имя."""
    shown = [
        handler.GREETING,
        threads.ASK_WHAT,
        threads.PUSHED_OUT,
        threads.LIST_HEADER,
        threads.LIST_HEADER_NEW,
        threads.NO_SEARCHES,
        threads.NOT_FOUND,
        threads.MONITORING_ENDED,
        threads.EDIT_PROMPT,
        keyboards.NEW_THREAD_LABEL,
        keyboards.SEARCHES_LABEL,
        keyboards.ALL_SEARCHES_LABEL,
        # Подписка и оплата называют тот же поиск: третье имя для него здесь не нужно.
        billing.DESCRIPTION,
        billing.THANKS,
        billing.ALREADY,
        billing.PAYLOAD_REFUSED,
        billing.PAYMENT_STRANDED,
    ]

    for text in shown:
        lowered = text.lower()
        assert "ветк" not in lowered, text
        assert "запрос" not in lowered.replace("/requests", ""), text


def test_the_greeting_does_not_promise_that_the_list_shows_everything() -> None:
    """Список обрезан пределом, и «показывает все» было неправдой."""
    assert "показывает все" not in handler.GREETING
    assert "/new" in handler.GREETING
    assert "/requests" in handler.GREETING


def test_the_prompt_for_a_new_search_mentions_voice() -> None:
    """Голосовое после `/new` работает, но человек об этом не знал."""
    assert "наговорите" in threads.ASK_WHAT


def test_the_notice_names_the_limit_from_the_domain_not_a_copy() -> None:
    crowded = QueryOverview(root=5, passport=bike(), monitoring="off")
    live = [QueryOverview(root=n, passport=bike(city=f"c{n}")) for n in range(1, 5)] + [crowded]

    notice = threads.pushed_out_notice(crowded, live)

    assert f"не больше {MAX_LIVE_THREADS} поисков" in notice


# ── меню без базы: что оно спрашивает у хранилища ───────────────────────────


class _Scope:
    """`session_scope()` без Postgres: коммит есть, базы нет."""

    async def __aenter__(self) -> _Scope:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _fake_storage(
    monkeypatch: pytest.MonkeyPatch, *, starting_new: bool, owned: dict[int, QueryOverview]
) -> list[tuple[str, int, int | None]]:
    asked: list[tuple[str, int, int | None]] = []

    class Repo:
        def __init__(self, _session: object) -> None:
            pass

        async def list_queries(self, user_id: int, *, limit: int | None = None) -> list[Any]:
            asked.append(("list", user_id, limit))
            return list(owned.values())

        async def get_query(self, user_id: int, root: int) -> QueryOverview | None:
            asked.append(("get", user_id, root))
            return owned.get(root)

    class Users:
        def __init__(self, _session: object) -> None:
            pass

        async def get_or_create(self, _tg_user_id: int, **_kwargs: object) -> Any:
            return type("U", (), {"id": 7, "awaiting_new_request": starting_new})()

    monkeypatch.setattr(query_menu, "PassportRepository", Repo)
    monkeypatch.setattr(query_menu, "UserRepository", Users)
    monkeypatch.setattr(query_menu, "session_scope", _Scope)
    return asked


async def test_the_menu_carries_the_armed_flag_of_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    item = QueryOverview(root=3, passport=bike())

    _fake_storage(monkeypatch, starting_new=True, owned={3: item})
    assert (await query_menu.list_for(CLIENT)).starting_new is True
    _fake_storage(monkeypatch, starting_new=False, owned={3: item})
    assert (await query_menu.list_for(CLIENT)).starting_new is False


async def test_the_menu_lists_with_the_default_limit_and_looks_one_up_by_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Список обрезан пределом репозитория; один поиск спрашивается по корню, без списка."""
    item = QueryOverview(root=3, passport=bike())
    asked = _fake_storage(monkeypatch, starting_new=False, owned={3: item})

    menu = await query_menu.list_for(CLIENT)
    found = await query_menu.get_one(CLIENT, 3)
    missing = await query_menu.get_one(CLIENT, 4)

    assert menu.items == [item]
    assert found == item
    assert missing is None
    kinds = [(kind, user_id) for kind, user_id, _third in asked]
    assert kinds == [("list", 7), ("get", 7), ("get", 7)]
    listed_limit = asked[0][2]
    assert listed_limit is None or listed_limit <= MAX_LIVE_THREADS, (
        "меню не вправе тянуть больше предела: он стоит в запросе, а не в отрисовке"
    )
    assert [third for _kind, _user, third in asked[1:]] == [3, 4], "один поиск — по корню"
