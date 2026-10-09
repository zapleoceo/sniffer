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
    -- Докуда дочитан АРХИВ, вниз от последнего сообщения. `last_msg_id`
    -- движется вперёд за новыми постами, этот — назад за старыми: без него
    -- чат отдавал бы только те 200 сообщений, что висели на момент
    -- вступления, а история в двадцать восемь тысяч оставалась бы недоступна.
    -- 0 = добор ещё не начинался.
    backfill_msg_id BIGINT    NOT NULL DEFAULT 0,
    -- Telegram отдал пустую страницу: начало чата достигнуто, больше не ходим.
    backfill_done BOOLEAN     NOT NULL DEFAULT FALSE,
    last_synced_at TIMESTAMPTZ,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Исключение по решению владельца (020_chat_exclusion.sql): обратимо, из группы не
    -- выходим. NULL в excluded_at — чат в обычной работе.
    excluded_at       TIMESTAMPTZ,
    excluded_reason   TEXT,
    excluded_evidence JSONB,
    -- Курсор проверки живости (021_liveness_cursor.sql): докуда по listing.id перечитан круг.
    liveness_listing_id BIGINT NOT NULL DEFAULT 0,
    -- Когда чат последним проходил проверку живости (024_liveness_checked_at.sql); NULL — ни разу.
    liveness_checked_at TIMESTAMPTZ
);

-- `CREATE TABLE IF NOT EXISTS` существующую таблицу НЕ трогает вовсе: на живой
-- базе колонки выше не появятся ни от одного повторного прогона. А процедура
-- обновления схемы в deploy.md состоит ровно из «прогнать этот файл заново»,
-- то есть без строк ниже она молча ничего бы не сделала, и коллектор упал бы
-- на первом же SELECT новой колонки. Идемпотентность файла — свойство, за
-- которое отвечает файл, а не память того, кто его запускает.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS backfill_msg_id BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS backfill_done   BOOLEAN NOT NULL DEFAULT FALSE;

-- Очередь кандидатов в реестр: чаты, на которые сослались изнутри других
-- чатов, находки поиска по словарю и стартовый набор из
-- docs/chats-nha-trang.md (см. 002_seed_candidates.sql).
--
-- Очередь именно в базе, а не в процессе. Ручное вступление отменено
-- (roadmap, волна 4.5), значит это единственный путь, каким проект набирает
-- чаты; при десяти вступлениях в скользящие сутки она живёт днями, и перезапуск
-- контейнера не должен стирать проделанный отбор.
CREATE TABLE IF NOT EXISTS chat_candidates (
    id           BIGSERIAL PRIMARY KEY,
    -- нормализованная ссылка: '@username' в нижнем регистре либо '+hash'.
    -- Хэш приглашения регистрозависим, поэтому в нижний регистр не приводится.
    key          TEXT        NOT NULL UNIQUE,
    username     TEXT,
    invite_hash  TEXT,
    -- где встретили: кандидат из плотной барахолки и кандидат из случайного
    -- чата стоят разного, а разбирать очередь вслепую невозможно
    found_in     TEXT        NOT NULL DEFAULT '',
    -- порядок разбора, меньше — раньше. Стартовые волны 10/20/30, находки
    -- разведки 100: автоматика не оттесняет то, что владелец выбрал руками
    priority     INT         NOT NULL DEFAULT 100,
    -- queued → joining → (строка удаляется при успехе либо возвращается в
    -- queued). 'joining' держит кандидата занятым, пока один воркер вступает:
    -- иначе второй возьмёт того же и потратит суточный лимит на чат, в
    -- котором мы уже состоим.
    status       TEXT        NOT NULL DEFAULT 'queued',
    -- сколько раз вступление в этого кандидата кончилось НЕИЗВЕСТНЫМ исходом.
    -- Живёт в базе, а не в памяти воркера: счётчик, обнуляемый перезапуском,
    -- не ограничивает ничего. Без него один кандидат съедал все три суточных
    -- слота бессрочно — очередь за ним не двигалась, а наружу уходило по три
    -- бесполезных join в день (замер: 21 за семь суток).
    attempts     INT         NOT NULL DEFAULT 0,
    found_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 023: превью t.me/<username> и класс (relevant | off_topic | foreign_city |
    -- unknown; NULL = не снимали). Только двигает порядок разбора, не отклоняет.
    preview_class      TEXT,
    preview_evidence   TEXT,
    preview_snapshot   JSONB,
    preview_checked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS chat_candidates_queue_idx
    ON chat_candidates (priority, found_at) WHERE status = 'queued';

-- Отклонённые кандидаты. Без этой таблицы каждая ссылка на дананговскую
-- барахолку стоила бы одного resolve_username при каждой встрече, а
-- встречается она в чатах постоянно. Причина хранится словом: «отклонён» без
-- причины через месяц невозможно ни перепроверить, ни отменить.
CREATE TABLE IF NOT EXISTS chat_rejects (
    key         TEXT        PRIMARY KEY,
    reason      TEXT        NOT NULL,
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Журнал вступлений — единственный источник правды по лимитам (CLAUDE.md,
-- «Работа с Telegram»). Именно в базе, а не в памяти процесса: процесс
-- перезапускается деплоем и падением, а лимит Telegram нет, и счётчик в
-- памяти разрешал бы десять вступлений после каждого рестарта.
--
-- Строка создаётся ДО вступления, одной транзакцией с проверкой всех ворот
-- (стоп после флуда, три за скользящие сутки, час паузы). Проверить, а потом
-- записать — это гонка: два воркера успевают пройти одни и те же ворота.
-- Поэтому слот сначала занимается, и только потом идёт запрос в Telegram.
--
-- Одна таблица на вступления и флуды: оба ограничивают одно и то же действие,
-- а две таблицы заставили бы читать обе, чтобы ответить «можно ли сейчас».
CREATE TABLE IF NOT EXISTS chat_join_events (
    id          BIGSERIAL PRIMARY KEY,
    -- claimed → joined | flood; строка со статусом claimed, оставшаяся после
    -- обрыва связи, намеренно продолжает занимать слот: недосчитать одно
    -- вступление безопаснее, чем сделать одиннадцатое
    kind        TEXT        NOT NULL DEFAULT 'claimed',
    tg_id       BIGINT,
    username    TEXT,
    happened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- не раньше этого момента можно вступать снова. Час плюс случайная
    -- добавка, посчитанная при занятии слота и записанная сюда, — а не
    -- пересчитанная при каждом опросе: иначе разброс превратился бы в ровный
    -- час, то есть в подпись автомата
    next_allowed_at TIMESTAMPTZ,
    -- kind='flood': до этого момента вступлений нет вовсе
    blocked_until   TIMESTAMPTZ,
    -- Беззвучный режим встал или нет. Провал не откатывает вступление (оно
    -- уже стоило суточного лимита), но и молча забыться не должен:
    -- незаглушенный чат чинится повтором по этому флагу.
    muted       BOOLEAN     NOT NULL DEFAULT FALSE,
    mute_error  TEXT
);

CREATE INDEX IF NOT EXISTS chat_join_events_recent_idx
    ON chat_join_events (happened_at DESC);
CREATE INDEX IF NOT EXISTS chat_join_events_unmuted_idx
    ON chat_join_events (tg_id) WHERE kind = 'joined' AND NOT muted;

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

-- Сырьё как пришло. Срок хранения 90 дней, считается от `ingested_at`;
-- убирает воркер (`worker/retention.py`), не системный крон: состояние машины
-- задаётся репозиторием, а строчка в чужом crontab не переживёт пересборку.
-- Срок был 30 дней и не исполнялся вообще — уборки не существовало до
-- 01.09.2026, а комментарий утверждал обратное.
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
    -- pending → extracted | rejected | duplicate (кросспост: карточка уже есть)
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
    raw_message_id  BIGINT      REFERENCES raw_messages(id) ON DELETE CASCADE,
    -- FK arrives in 005 after catalog_observations exists.
    catalog_observation_id BIGINT,
    source          TEXT        NOT NULL DEFAULT 'telegram_archive',
    external_id     TEXT,
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
    screened_at     TIMESTAMPTZ,            -- ИИ-проверка, см. 009_listing_screen.sql
    screen_note     TEXT,
    -- Когда и почему снята с выдачи (022_deactivation_reason.sql); NULL = неизвестно.
    deactivated_at     TIMESTAMPTZ,
    deactivated_reason TEXT,

    search_tsv      tsvector,
    embedding       vector(1024),

    UNIQUE (raw_message_id)
);

ALTER TABLE listings ALTER COLUMN raw_message_id DROP NOT NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'telegram_archive';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS listings_source_external_idx
    ON listings (source, external_id);

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
    is_blocked  BOOLEAN     NOT NULL DEFAULT FALSE,
    active_passport_root  BIGINT,
    editing_passport_root BIGINT,
    awaiting_new_request  BOOLEAN NOT NULL DEFAULT FALSE,
    bot_blocked_at        TIMESTAMPTZ,  -- клиент заблокировал бота, см. 014_notifier_safety.sql
    -- Якорь квоты и метка последнего предложения подписки: 010_quota_ledger.sql.
    quota_anchor_at       TIMESTAMPTZ,
    paywall_offered_at    TIMESTAMPTZ
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS active_passport_root BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS editing_passport_root BIGINT;
-- `/new` без текста: следующее сообщение открывает новую ветку, чем бы оно ни
-- было похоже на прежнюю просьбу. В БД, а не в памяти процесса, по той же
-- причине, по которой тут лежит editing_passport_root: между командой и
-- сообщением (а при голосовом запросе — и расшифровкой) бот перезапускается, и
-- тогда явное «начинаю новый поиск» снова решалось бы эвристикой.
ALTER TABLE users ADD COLUMN IF NOT EXISTS awaiting_new_request BOOLEAN NOT NULL DEFAULT FALSE;
-- Клиент заблокировал бота: писать ему нельзя (HTTP 403 на sendMessage или апдейт
-- my_chat_member). ALTER повторён в 014_notifier_safety.sql: колонку обязан получить
-- любой, кто применил хотя бы один из двух файлов, иначе нотифаер падает на первом проходе.
ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_blocked_at TIMESTAMPTZ;

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
    is_current   BOOLEAN     NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ
);

-- Когда поиск использовали в последний раз: выбрали в `/requests`, нажали
-- кнопку под его выдачей, поправили. По этому времени, а не по времени правки,
-- упорядочен список поисков: иначе выбор вытесненного поиска не возвращал бы его
-- в список, хотя бот обещает именно это. NULL — с появления колонки поиск не
-- трогали; тогда порядок даёт created_at, то есть прежний.
ALTER TABLE passports ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS passports_user_idx ON passports (user_id, is_current);
CREATE INDEX IF NOT EXISTS passports_root_idx ON passports (root_id, version DESC);

-- Ключ цепочки — COALESCE(root_id, id): у первой версии root_id пуст, корнем
-- ей служит свой же id. Оба индекса уникальны не для скорости, а как последний
-- барьер: приложение блокирует корень цепочки перед вставкой, но два
-- одновременных уточнения (двойной тап, ретрай апдейта, два воркера) не должны
-- уметь оставить в цепочке ни двух версий с одним номером, ни двух актуальных
-- — «последняя версия» после такого перестаёт быть определённой, а подписка и
-- аналитика начинают читать разное.
CREATE UNIQUE INDEX IF NOT EXISTS passports_chain_version_idx
    ON passports ((COALESCE(root_id, id)), version);
CREATE UNIQUE INDEX IF NOT EXISTS passports_chain_current_idx
    ON passports ((COALESCE(root_id, id))) WHERE is_current;

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

-- Список видов закрыт: из этого лога собирается состояние диалога, и опечатка
-- в виде события даёт не ошибку, а молча потерянный вопрос. Отдельным ALTER, а
-- не строкой в CREATE TABLE, — чтобы ограничение доехало и до баз, созданных
-- прежней версией файла.
ALTER TABLE passport_events DROP CONSTRAINT IF EXISTS passport_events_kind_check;
ALTER TABLE passport_events ADD CONSTRAINT passport_events_kind_check
    CHECK (kind IN ('user_message', 'feedback', 'agent_infer', 'manual_edit', 'question_asked'));

-- ── подписки и доставка ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscriptions (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    passport_root  BIGINT      NOT NULL,
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    -- instant | digest — при digest даже высокий score копится до сводки
    mode           TEXT        NOT NULL DEFAULT 'instant',
    max_per_day    INT         NOT NULL DEFAULT 10,
    quiet_from     TIME,
    quiet_to       TIME,
    sent_today     INT         NOT NULL DEFAULT 0,
    day_bucket     DATE,
    -- Подписка платная: 1 звезда Telegram в месяц. Ниже те же колонки стоят
    -- отдельными ALTER — для живой базы, где CREATE TABLE IF NOT EXISTS не
    -- сработает (deploy.md 7.1).
    since_listing_id BIGINT    NOT NULL DEFAULT 0,
    scan_listing_id  BIGINT    NOT NULL DEFAULT 0,
    expires_at     TIMESTAMPTZ,
    charge_id      TEXT,
    -- Состояние монитора (worker/matcher.py, docs/architecture.md 7.1): ротация обхода
    -- и карантин сбойной подписки. Ниже те же колонки стоят отдельными ALTER.
    last_scanned_at   TIMESTAMPTZ,
    failed_streak     INT         NOT NULL DEFAULT 0,
    last_error        TEXT,
    quarantined_until TIMESTAMPTZ,
    -- Монитор по слоту (015_monitor_agent.sql): сводка «ещё N» и пауза без слота.
    suppressed_total  INT         NOT NULL DEFAULT 0,
    overflow_day      DATE,
    overflow_count    INT         NOT NULL DEFAULT 0,
    overflow_notified BOOLEAN     NOT NULL DEFAULT FALSE,
    no_slot_since     TIMESTAMPTZ,
    -- Порядок претензии на слот мониторинга (016_stars_slots.sql).
    priority       INT         NOT NULL DEFAULT 0,
    -- Жёсткие условия подписки (018_subscription_hard_filter.sql); NULL — обычный отбор.
    hard_filter    JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, passport_root)
);

-- Дедуп доставки: одно объявление уходит в одну подписку ровно один раз,
-- даже если матчер отработал по нему дважды после рестарта.
-- Подписка платная: 1 звезда Telegram в месяц. Колонки добавлены отдельными
-- ALTER, потому что CREATE TABLE IF NOT EXISTS существующую таблицу не трогает
-- (deploy.md 7.1).
--
-- `since_listing_id` — с какой карточки начинается слежение. Без него свежая
-- подписка вываливает клиенту весь двухнедельный запас разом, включая ровно те
-- объявления, которые он только что посмотрел и не выбрал. Подписка обещает
-- НОВЫЕ посты, а не пересказ выдачи.
--
-- `charge_id` — идентификатор платежа Telegram. Он же ключ отмены через
-- `editUserStarSubscription`, поэтому хранится, а не выбрасывается.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS since_listing_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS scan_listing_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS charge_id TEXT;

-- Состояние монитора подписок. `last_scanned_at` — когда монитор последний раз брал
-- подписку в обход: по нему обход идёт по кругу («кого не смотрели дольше всех —
-- первым»), NULL = ещё ни разу, такие первыми. Это ключ ротации, а не «когда
-- последний раз всё удалось»: подписка, пропущенная из-за недоступного курса, тоже
-- считается обойдённой, иначе пропускаемые заняли бы всю порцию и заморили остальных.
-- `failed_streak`/`last_error`/`quarantined_until` — карантин: сколько проходов подряд
-- подписка падала, чем и до какого времени её не трогаем. Успешный проход всё это
-- обнуляет. Данные не правим: DEFAULT 0 и NULL — состояние «не падала».
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_scanned_at TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS failed_streak INT NOT NULL DEFAULT 0;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quarantined_until TIMESTAMPTZ;

-- Деньги. Отдельной таблицей, а не колонкой в подписке: у одной подписки
-- платежей столько, сколько месяцев её продлевали, и продление обязано
-- оставлять след, даже если подписку потом выключили.
--
-- `external_id` уникален намеренно и это главное свойство таблицы. Telegram
-- ПОВТОРЯЕТ апдейт, если бот не ответил вовремя, и без уникальности один
-- платёж продлил бы подписку дважды. Идемпотентность здесь не украшение:
-- деньги нельзя обработать «примерно один раз».
CREATE TABLE IF NOT EXISTS payments (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id BIGINT      REFERENCES subscriptions(id) ON DELETE SET NULL,
    provider        TEXT        NOT NULL DEFAULT 'telegram_stars',
    amount          INT         NOT NULL,
    currency        TEXT        NOT NULL DEFAULT 'XTR',
    -- paid | refunded
    status          TEXT        NOT NULL DEFAULT 'paid',
    -- telegram_payment_charge_id: уникален у Telegram, уникален и у нас
    external_id     TEXT        NOT NULL UNIQUE,
    is_recurring    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Оплата звёздами: что за платёж, чей, по какому счёту и до какого срока.
    -- См. 011_stars_billing.sql — там же ALTER для живой базы.
    tg_user_id      BIGINT,
    invoice_payload TEXT,
    kind            TEXT CHECK (kind IN ('first', 'renewal', 'one_off', 'duplicate', 'unknown')),
    is_first_recurring BOOLEAN  NOT NULL DEFAULT FALSE,
    period_end      TIMESTAMPTZ,
    refunded_at     TIMESTAMPTZ,
    raw             JSONB,
    -- Слоты (016_stars_slots.sql): откуда запись и оценка ли срок.
    source          TEXT        NOT NULL DEFAULT 'update',
    period_end_estimated BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS payments_user_idx ON payments (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id              BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT      NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    listing_id      BIGINT      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    score           REAL        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    UNIQUE (subscription_id, listing_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id BIGINT   REFERENCES subscriptions(id) ON DELETE CASCADE,
    notification_id BIGINT   UNIQUE REFERENCES notifications(id) ON DELETE CASCADE,
    payload      JSONB       NOT NULL,
    -- pending|sent|failed|cancelled. cancelled — право на сообщение кончилось раньше, чем оно
    -- ушло (подписка истекла, льгота прошла): строку не удаляем, чтобы карточка повторно не
    -- ставилась и причина молчания читалась по базе.
    status       TEXT        NOT NULL DEFAULT 'pending',
    attempts     INT         NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at      TIMESTAMPTZ,
    last_error   TEXT         -- почему строка не ушла: отмена, отказ Telegram, последний сбой
);

CREATE INDEX IF NOT EXISTS outbox_due_idx ON outbox (status, scheduled_at);

-- Старые базы создавали notification как уже отправленную при постановке в
-- очередь и не связывали outbox с причиной. Новые колонки доезжают повторным
-- прогоном этого же файла; старые строки остаются историческим фактом.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
UPDATE notifications SET created_at = COALESCE(sent_at, now()) WHERE created_at IS NULL;
ALTER TABLE notifications ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE notifications ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE notifications ALTER COLUMN sent_at DROP NOT NULL;
ALTER TABLE notifications ALTER COLUMN sent_at DROP DEFAULT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE CASCADE;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS notification_id BIGINT UNIQUE REFERENCES notifications(id) ON DELETE CASCADE;
-- Причина, по которой строка не ушла (ALTER повторён в 014_notifier_safety.sql).
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_error TEXT;

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

-- ── наблюдаемость и сессии ──────────────────────────────────────────────────
-- Всё, что показывает веб-интерфейс владельца (docs/dashboard.md). Данные
-- пишем свои: страница не должна зависеть от доступности брокера.

-- Один запрос клиента как единица наблюдения. Существует ровно затем, чтобы
-- «клиент спросил про байк» и «потрачено N токенов» связывались ключом, а не
-- сопоставлением времени — при двух параллельных запросах время врёт.
CREATE TABLE IF NOT EXISTS client_requests (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    passport_id   BIGINT      REFERENCES passports(id) ON DELETE SET NULL,
    raw_query     TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'running',  -- running|done|failed
    -- Куда ушло время: {"intake_ms": 1200, "plan_ms": 900, "search_ms": 8000}.
    -- JSONB, а не колонки: ступеней у воронки станет больше, и каждая новая не
    -- должна требовать миграции ради одного числа.
    stages        JSONB       NOT NULL DEFAULT '{}',
    plan_fallback BOOLEAN     NOT NULL DEFAULT FALSE,
    sources       TEXT[]      NOT NULL DEFAULT '{}',
    result_count  INT         NOT NULL DEFAULT 0,
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    duration_ms   INT,
    -- Показано и удержано лимитом квоты: 010_quota_ledger.sql.
    shown_count   INT         NOT NULL DEFAULT 0,
    withheld_count INT        NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS client_requests_user_idx   ON client_requests (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS client_requests_recent_idx ON client_requests (started_at DESC);

-- Переписка с ботом. Отдельно от `passport_events`: события паспорта — это
-- история разбора запроса, а здесь лежит то, что человек реально увидел.
CREATE TABLE IF NOT EXISTS dialog_messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id BIGINT      REFERENCES client_requests(id) ON DELETE SET NULL,
    direction  TEXT        NOT NULL,                       -- in | out
    text       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dialog_messages_user_idx ON dialog_messages (user_id, created_at DESC);

-- Расходы на LLM. `broker_request_id` — это id строки в `usage_log` брокера,
-- он же приезжает в ответе завершённой задачи. Держим его, чтобы спор о
-- стоимости решался сверкой по ключу, а не пересказом.
CREATE TABLE IF NOT EXISTS broker_calls (
    id                BIGSERIAL PRIMARY KEY,
    request_id        BIGINT        REFERENCES client_requests(id) ON DELETE SET NULL,
    broker_request_id BIGINT,
    capability        TEXT          NOT NULL,
    provider          TEXT,
    model             TEXT,
    tokens_in         INT           NOT NULL DEFAULT 0,
    tokens_out        INT           NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    latency_ms        INT,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS broker_calls_request_idx ON broker_calls (request_id);
-- Поллинг может вернуть один и тот же завершённый job дважды (ретрай сети,
-- рестарт процесса) — двойной записи расхода быть не должно. Индекс частичный:
-- у вызова, до которого брокер не дошёл, ключа нет, и такие строки не спорят.
CREATE UNIQUE INDEX IF NOT EXISTS broker_calls_broker_id_idx
    ON broker_calls (broker_request_id) WHERE broker_request_id IS NOT NULL;

-- Сессия юзербота. Строка сессии лежит зашифрованной (Fernet, ключ из
-- SECRET_ENCRYPTION_KEY): дамп базы не должен давать доступ к аккаунту.
CREATE TABLE IF NOT EXISTS telegram_sessions (
    id            BIGSERIAL PRIMARY KEY,
    phone         TEXT        NOT NULL UNIQUE,
    session_enc   TEXT        NOT NULL,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    last_ok_at    TIMESTAMPTZ,
    last_error    TEXT,
    last_error_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
