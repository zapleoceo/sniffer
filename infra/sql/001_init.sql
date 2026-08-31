-- SnifferBot — начальная схема.
-- Идемпотентна: безопасно применять повторно.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- ── источники ───────────────────────────────────────────────────────────────

-- Справочник отслеживаемых сообществ. Курируется вручную: качество этого
-- списка влияет на продукт сильнее, чем весь остальной код.
CREATE TABLE IF NOT EXISTS chats (
    id            BIGSERIAL PRIMARY KEY,
    tg_id         BIGINT      NOT NULL UNIQUE,
    username      TEXT,
    title         TEXT        NOT NULL,
    city          TEXT        NOT NULL,
    categories    TEXT[]      NOT NULL DEFAULT '{}',
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    -- приоритет для живого поиска: за один запрос обходим не больше 10 чатов,
    -- начиная с самых плотных по объявлениям
    search_rank   INT         NOT NULL DEFAULT 100,
    msg_count_24h INT         NOT NULL DEFAULT 0,
    -- На чём остановился сбор по этому чату. Telegram не держит для нас
    -- очередь: после любого простоя (выключенный ноутбук, деплой, падение
    -- коллектора) догоняем историю с этого msg_id, иначе объявления за время
    -- простоя пропадают молча.
    last_msg_id   BIGINT      NOT NULL DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sellers (
    id            BIGSERIAL PRIMARY KEY,
    tg_user_id    BIGINT      NOT NULL UNIQUE,
    display_name  TEXT,
    posts_30d     INT         NOT NULL DEFAULT 0,
    distinct_chats INT        NOT NULL DEFAULT 0,
    -- 0 = чисто, 1 = почти наверняка спамер/скамер
    scam_score    REAL        NOT NULL DEFAULT 0,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Сырьё как пришло. TTL 30 дней — чистится кроном, иначе съест диск.
CREATE TABLE IF NOT EXISTS raw_messages (
    id           BIGSERIAL PRIMARY KEY,
    chat_tg_id   BIGINT      NOT NULL,
    msg_id       BIGINT      NOT NULL,
    seller_id    BIGINT      REFERENCES sellers(id) ON DELETE SET NULL,
    text         TEXT        NOT NULL,
    -- sha256 нормализованного текста: ловит кросспостинг одного объявления
    -- в пять групп, где меняются только эмодзи и пробелы
    text_hash    TEXT        NOT NULL,
    has_media    BOOLEAN     NOT NULL DEFAULT FALSE,
    posted_at    TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- pending → gated → prefiltered → extracted → rejected
    stage        TEXT        NOT NULL DEFAULT 'pending',
    gate_signals JSONB       NOT NULL DEFAULT '{}',
    UNIQUE (chat_tg_id, msg_id)
);

CREATE INDEX IF NOT EXISTS raw_messages_stage_idx    ON raw_messages (stage, posted_at DESC);
CREATE INDEX IF NOT EXISTS raw_messages_hash_idx     ON raw_messages (text_hash);
CREATE INDEX IF NOT EXISTS raw_messages_posted_idx   ON raw_messages (posted_at);

-- ── предложения ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS listings (
    id              BIGSERIAL PRIMARY KEY,
    raw_message_id  BIGINT      NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    seller_id       BIGINT      REFERENCES sellers(id) ON DELETE SET NULL,

    deal_type       TEXT        NOT NULL,          -- sell | rent_out | wanted
    category        TEXT        NOT NULL,
    city            TEXT        NOT NULL,
    district        TEXT,

    title           TEXT        NOT NULL,
    summary         TEXT        NOT NULL,
    -- цена всегда хранится и как есть, и приведённой к USD: сравнивать
    -- 7 triệu с 300$ иначе невозможно
    price_amount    NUMERIC(14,2),
    price_currency  TEXT,
    price_period    TEXT,                          -- once | day | week | month
    price_usd_month NUMERIC(14,2),

    attributes      JSONB       NOT NULL DEFAULT '{}',
    tg_link         TEXT        NOT NULL,
    lang            TEXT,
    confidence      REAL        NOT NULL DEFAULT 0,

    posted_at       TIMESTAMPTZ NOT NULL,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,

    search_tsv      tsvector,
    embedding       vector(1024),

    UNIQUE (raw_message_id)
);

-- Составной индекс под жёсткие фильтры матчинга — именно в этом порядке
-- ходит и разовый подбор, и проверка подписок.
CREATE INDEX IF NOT EXISTS listings_match_idx
    ON listings (city, category, deal_type, is_active, posted_at DESC);
CREATE INDEX IF NOT EXISTS listings_price_idx  ON listings (price_usd_month);
CREATE INDEX IF NOT EXISTS listings_tsv_idx    ON listings USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS listings_trgm_idx   ON listings USING GIN (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS listings_attrs_idx  ON listings USING GIN (attributes);

CREATE TABLE IF NOT EXISTS listing_media (
    id          BIGSERIAL PRIMARY KEY,
    listing_id  BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    r2_key      TEXT   NOT NULL,
    width       INT,
    height      INT,
    bytes       INT,
    UNIQUE (listing_id, r2_key)
);

-- ── клиенты и паспорта ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    tg_user_id  BIGINT      NOT NULL UNIQUE,
    username    TEXT,
    lang        TEXT        NOT NULL DEFAULT 'ru',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_blocked  BOOLEAN     NOT NULL DEFAULT FALSE
);

-- Паспорт неизменяем: правка поля создаёт новую версию с тем же root_id.
CREATE TABLE IF NOT EXISTS passports (
    id           BIGSERIAL PRIMARY KEY,
    root_id      BIGINT,                            -- NULL у первой версии
    version      INT         NOT NULL DEFAULT 1,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT        NOT NULL DEFAULT 'draft',
    intent       TEXT,
    category     TEXT,
    city         TEXT,
    districts    TEXT[]      NOT NULL DEFAULT '{}',
    budget       JSONB       NOT NULL DEFAULT '{}',
    attributes   JSONB       NOT NULL DEFAULT '{}',
    must_have    TEXT[]      NOT NULL DEFAULT '{}',
    deal_breakers TEXT[]     NOT NULL DEFAULT '{}',
    timeframe_from DATE,
    timeframe_to   DATE,
    raw_query    TEXT        NOT NULL,
    confidence   REAL        NOT NULL DEFAULT 0,
    missing_fields TEXT[]    NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current   BOOLEAN     NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS passports_user_idx ON passports (user_id, is_current);
CREATE INDEX IF NOT EXISTS passports_root_idx ON passports (root_id, version DESC);

CREATE TABLE IF NOT EXISTS passport_events (
    id           BIGSERIAL PRIMARY KEY,
    passport_id  BIGINT      NOT NULL REFERENCES passports(id) ON DELETE CASCADE,
    -- user_message | feedback | agent_infer | manual_edit | question_asked
    -- question_asked — не правка паспорта, а след диалога: из него собирается
    -- счётчик заданных вопросов, который обязан пережить перезапуск бота.
    kind         TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── подписки и доставка ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscriptions (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    passport_root  BIGINT      NOT NULL,
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    -- instant | digest — при digest даже высокий score копится до сводки
    mode           TEXT        NOT NULL DEFAULT 'instant',
    max_per_day    INT         NOT NULL DEFAULT 5,
    quiet_from     TIME,
    quiet_to       TIME,
    sent_today     INT         NOT NULL DEFAULT 0,
    day_bucket     DATE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, passport_root)
);

-- Дедуп доставки: одно объявление уходит в одну подписку ровно один раз,
-- даже если матчер отработал по нему дважды после рестарта.
CREATE TABLE IF NOT EXISTS notifications (
    id              BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT      NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    listing_id      BIGINT      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    score           REAL        NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subscription_id, listing_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payload      JSONB       NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    attempts     INT         NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox (status, scheduled_at);

-- ── внутренняя очередь ──────────────────────────────────────────────────────

-- Очередь на Postgres вместо Redis/Rabbit: разбирается через
-- SELECT … FOR UPDATE SKIP LOCKED. На этом объёме брокер сообщений — лишняя
-- операционная сложность без единой решаемой проблемы.
CREATE TABLE IF NOT EXISTS jobs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    status       TEXT        NOT NULL DEFAULT 'pending',
    attempts     INT         NOT NULL DEFAULT 0,
    locked_at    TIMESTAMPTZ,
    run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (status, run_after, id);
