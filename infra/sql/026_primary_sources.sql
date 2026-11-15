-- Первоисточники и дедуп на входе (docs/primary-sources.md).
--
-- raw_messages.origin_*: откуда сообщение на самом деле. NULL в origin_kind — сообщение
-- само оригинал; 'forward' — пересылка с известным адресом оригинала (канал + номер
-- поста); 'forward_hidden' — пересылка без адреса; 'pointer' — сообщение-ссылка на
-- чужой пост или внешний адрес. Последние два своей карточки не получают.
-- raw_messages.identity_hash: отпечаток текста без цены — ловит перепост с новой ценой.
--
-- catalog_publications.identity_keys: ключи личности проверенной публикации (ссылка на
-- первоисточник, отпечаток текста, отпечаток без цены). duplicate_of_*: публикация —
-- дубль другой и в выдачу не идёт.
--
-- listing_cleanup_events: журнал уборки (worker/primary_cleanup.py) — снимок карточки
-- «как было» до переписывания ссылки, слияния или снятия. Только добавление.
--
-- Только идемпотентный DDL (tests/test_sql_chain.py): данные заполняет коллектор для
-- новых сообщений и уборка — по решению владельца. Колонки есть и в телах CREATE TABLE
-- (raw_messages — 001_init.sql, catalog_publications — 003); ALTER ниже — для живой базы.
ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS origin_kind       TEXT;
ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS origin_chat_tg_id BIGINT;
ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS origin_msg_id     BIGINT;
ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS origin_username   TEXT;
ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS identity_hash     TEXT;
CREATE INDEX IF NOT EXISTS raw_messages_identity_idx
    ON raw_messages (identity_hash) WHERE identity_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS raw_messages_origin_idx
    ON raw_messages (origin_chat_tg_id, origin_msg_id) WHERE origin_msg_id IS NOT NULL;

ALTER TABLE catalog_publications ADD COLUMN IF NOT EXISTS identity_keys TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE catalog_publications ADD COLUMN IF NOT EXISTS duplicate_of_source      TEXT;
ALTER TABLE catalog_publications ADD COLUMN IF NOT EXISTS duplicate_of_external_id TEXT;
CREATE INDEX IF NOT EXISTS catalog_publications_identity_idx
    ON catalog_publications USING GIN (identity_keys);

CREATE TABLE IF NOT EXISTS listing_cleanup_events (
    id                   BIGSERIAL PRIMARY KEY,
    listing_id           BIGINT      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    canonical_listing_id BIGINT      REFERENCES listings(id) ON DELETE SET NULL,
    action               TEXT        NOT NULL,
    before               JSONB       NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS listing_cleanup_events_listing_idx
    ON listing_cleanup_events (listing_id);
