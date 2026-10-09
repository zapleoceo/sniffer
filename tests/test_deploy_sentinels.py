"""Часовые деплоя: «свежий ALTER и новая таблица — своя строка» держит CI, а не память.

Часовой — строка таблицы `schema_sentinels` в `infra/deploy.sh`: `таблица колонка`
для колонки из `ALTER TABLE … ADD COLUMN IF NOT EXISTS` и `таблица` для новой
таблицы целиком. Он валит деплой, если миграция не доехала до живой базы (живой
отказ 02.09.2026: таблицы были, колонок не было, а деплой рапортовал успех).
Охрана у такой проверки хрупкая в четырёх местах, и каждое было найдено пробной
мутацией, а не чтением:

* часовой заменили константой (`HAS_NEW=1`) — проверка исчезла, деплой зелёный;
* в имени колонки опечатка, записанная в другом регистре, — деплой вечно красный;
* появилась новая колонка или таблица, а строки на ней нет — и никто не заметил;
* строки на месте, а цикл, который их читает, никто не вызывает.

Поэтому здесь проверяется не вид строк, а устройство. Строки читаются из самого
скрипта и сверяются с моделями и с DDL ИМЕННО ЭТОЙ таблицы. Любой ALTER и любая
таблица, которых не было на момент введения правила, обязаны иметь строку
(храповик: списки старых закрыты и только сокращаются). Саму функцию и цикл
исполняет `test_deploy_sentinel_run.py` на подставном `docker`.
"""

from __future__ import annotations

import re
from pathlib import Path

from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.db.models import Base
from tests.deploy_support import Row, script, sentinel_rows
from tests.shell_support import function_source
from tests.sql_chain_support import SQL_DIR, added_columns, created_tables

# Часовые, которые стояли в деплое ДО таблицы (отдельными вызовами и блоком
# «хвост цепочки»). Таблица их заменила, и ни одна проверка не должна потеряться:
# убрать строку можно только вместе с самой колонкой или таблицей.
ORIGINAL = frozenset(
    {
        ("listings", "source"),
        ("users", "awaiting_new_request"),
        ("passports", "last_used_at"),
        ("schema_proposals", None),
    }
)

# ALTER'ы, существовавшие к введению правила, у которых часового нет. Список
# ЗАКРЫТ: новая строка в него — это обход правила, и ревью обязано её заметить.
# Список только сокращается: появился часовой — строка отсюда уходит, иначе
# `test_the_legacy_lists_only_name_real_unguarded_objects` краснеет.
LEGACY_WITHOUT_SENTINEL = frozenset(
    {
        "chats.backfill_msg_id",
        "chats.backfill_done",
        "listings.external_id",
        "listings.catalog_observation_id",
        "listings.screened_at",
        "listings.screen_note",
        "users.active_passport_root",
        "users.editing_passport_root",
        "subscriptions.since_listing_id",
        "subscriptions.scan_listing_id",
        "subscriptions.expires_at",
        "subscriptions.charge_id",
        "notifications.created_at",
        "outbox.subscription_id",
        "outbox.notification_id",
        "collection_subscribers.reply_queued_at",
    }
)

# Таблицы, существовавшие к введению правила, без строки. Закрыт по той же
# причине и сокращается так же. Таблица, которой здесь нет, обязана попасть в
# `schema_sentinels` в том же коммите, что и её DDL.
LEGACY_TABLES_WITHOUT_SENTINEL = frozenset(
    {
        "broker_calls",
        "catalog_coverage",
        "catalog_observations",
        "catalog_publications",
        "chat_candidates",
        "chat_join_events",
        "chat_rejects",
        # chats охраняется колонками excluded_* (019), поэтому из списка ушёл
        "collection_actions",
        "collection_subscribers",
        "collection_tasks",
        "dialog_messages",
        "jobs",
        "listing_media",
        "notifications",
        "passport_events",
        "raw_messages",
        # subscriptions, outbox, client_requests и payments охраняются колонками в deploy.sh
        "sellers",
        "telegram_sessions",
    }
)


def unguarded(directory: Path, rows: list[Row]) -> tuple[list[str], list[str]]:
    """Что цепочка добавляет без часового: колонки `таблица.колонка` и таблицы."""
    guarded_columns: set[tuple[str, str]] = {(t, c) for t, c in rows if c is not None}
    guarded_tables = {table for table, _ in rows}
    columns = sorted(f"{t}.{c}" for t, c in added_columns(directory) - guarded_columns)
    tables = sorted(created_tables(directory) - guarded_tables)
    return (
        [name for name in columns if name not in LEGACY_WITHOUT_SENTINEL],
        [name for name in tables if name not in LEGACY_TABLES_WITHOUT_SENTINEL],
    )


# ── что часовой называет ────────────────────────────────────────────────────


def test_the_script_has_sentinels_at_all() -> None:
    assert sentinel_rows(), "часовых не нашлось — проверка охраняет пустоту"


def test_the_table_keeps_the_sentinels_that_existed_before_it() -> None:
    """Таблица заменила отдельные вызовы и блок «хвоста цепочки»: проверки не теряются."""
    lost = ORIGINAL - set(sentinel_rows())

    assert not lost, f"пропал часовой, стоявший в деплое до таблицы: {sorted(lost, key=str)}"


def test_there_is_exactly_one_column_probe_and_it_is_the_function() -> None:
    """Вторая копия запроса в скрипте — это проверка, которую можно ослабить отдельно.

    Именно так жила константа `HAS_NEW=1`: блок был копией, и подменить одну копию
    не стоило ничего. Запрос к `information_schema.columns` один — в функции.
    """
    probe = "information_schema.columns"
    guard = function_source(script(), "require_column")

    assert script().count(probe) == guard.count(probe) == 1, (
        "запрос к information_schema.columns вне require_column: часовой-копия"
    )


def test_every_sentinel_names_a_model_object_that_the_chain_adds() -> None:
    """Опечатка делает часового вечно красным, переименование — вечно зелёным.

    ALTER сверяется ПО ТАБЛИЦЕ: `users.created_at` прошёл бы сверку «где-нибудь есть
    ADD COLUMN created_at» за счёт `notifications`, хотя на `users` такого ALTER нет
    и часовой охранял бы чужую колонку. Часовой на таблице целиком обязан ссылаться
    на таблицу, которую цепочка действительно создаёт.
    """
    alters, created = added_columns(), created_tables()

    for table, column in sentinel_rows():
        assert table in Base.metadata.tables, f"{table}: нет такой таблицы в моделях"
        if column is None:
            assert table in created, (
                f"{table}: ни одного `CREATE TABLE IF NOT EXISTS {table}` — "
                "часовой охраняет несуществующее"
            )
            continue
        assert column in {c.name for c in Base.metadata.tables[table].columns}, (
            f"{table}.{column}: нет в моделях"
        )
        assert (table, column) in alters, (
            f"{table}.{column}: ни одного `ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            f"{column}` — на свежей базе часовой зелёный, на живой красный"
        )


def test_a_sentinel_is_not_listed_twice() -> None:
    rows = sentinel_rows()

    assert len(rows) == len(set(rows)), "один и тот же часовой дважды"


# ── храповик: свежий ALTER или таблица без часового краснит сборку ───────────


def test_a_fresh_alter_without_a_sentinel_turns_the_build_red() -> None:
    """Правило «новая колонка — своя строка» исполняется, а не помнится.

    «Свежий» — любой ALTER, которого нет в закрытом списке старых. Забыл строку —
    упал этот тест, и сообщение называет, что дописать. Убрали существующую —
    упал он же: колонка снова «свежая и без охраны».
    """
    columns, _ = unguarded(SQL_DIR, sentinel_rows())

    assert not columns, (
        "ALTER без часового: "
        + ", ".join(columns)
        + " — допишите в `schema_sentinels` (infra/deploy.sh) строку `<таблица> <колонка>`"
    )


def test_a_fresh_table_without_a_sentinel_turns_the_build_red() -> None:
    """То же для таблиц: деньги и права живут в НОВЫХ таблицах, а «таблиц ≥ 5» их не видит."""
    _, tables = unguarded(SQL_DIR, sentinel_rows())

    assert not tables, (
        "таблица без часового: "
        + ", ".join(tables)
        + " — допишите в `schema_sentinels` (infra/deploy.sh) строку `<таблица>`"
    )


def test_the_legacy_lists_only_name_real_unguarded_objects() -> None:
    """Закрытые списки не врут: в них нет ни выдуманных, ни уже охраняемых строк."""
    rows = sentinel_rows()
    alters = {f"{table}.{column}" for table, column in added_columns()}
    guarded = {f"{table}.{column}" for table, column in rows if column}
    guarded_tables = {table for table, _ in rows}

    assert LEGACY_WITHOUT_SENTINEL <= alters, sorted(LEGACY_WITHOUT_SENTINEL - alters)
    assert not LEGACY_WITHOUT_SENTINEL & guarded, "часовой появился — уберите строку из списка"
    assert LEGACY_TABLES_WITHOUT_SENTINEL <= created_tables()
    assert not LEGACY_TABLES_WITHOUT_SENTINEL & guarded_tables, "часовой появился — уберите таблицу"


def test_the_ratchets_see_a_table_and_a_column_from_a_file_numbered_010(tmp_path: Path) -> None:
    """Подставной `010_probe.sql`: за двузначной маской новая таблица осталась бы без часового."""
    (tmp_path / "010_probe.sql").write_text(
        "CREATE TABLE IF NOT EXISTS probe_things (\n    id BIGINT\n);\n"
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS probe_flag BOOLEAN;\n",
        encoding="utf-8",
    )

    assert unguarded(tmp_path, []) == (["users.probe_flag"], ["probe_things"])
    assert unguarded(tmp_path, [("probe_things", None), ("users", "probe_flag")]) == ([], [])


# ── устройство: часовые не обходятся и не остаются без вызова ────────────────


def test_a_sentinel_is_called_from_exactly_one_place_and_it_is_the_loop() -> None:
    """Прямой вызов `require_column` вне цикла — второй способ завести часового, то есть копия."""
    calls = re.findall(r"^[ \t]*require_column[ \t].*$", script(), re.MULTILINE)
    loop = function_source(script(), "check_schema_sentinels")

    assert len(calls) == 1 and calls[0].strip() in loop, (
        "часовые заводятся строками `schema_sentinels`, а не вызовами: перенесите "
        + str(calls)
        + " в таблицу строкой `<таблица> <колонка>` (или `<таблица>`)"
    )


def test_the_functional_check_runs_the_loop() -> None:
    """Строки без вызова цикла — таблица в пустоту: деплой зелёный при любой базе."""
    calls = re.findall(r"^[ \t]+check_schema_sentinels[ \t]*$", script(), re.MULTILINE)

    assert len(calls) == 1, f"цикл часовых вызывается {len(calls)} раз, ожидался ровно один"
