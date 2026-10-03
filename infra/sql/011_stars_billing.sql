-- Оплата звёздами Telegram: журнал платежей, согласие с условиями, события подписки.
--
-- Деплой гонит каждый файл цепочки на КАЖДОМ запуске, поэтому здесь только
-- идемпотентный DDL и ни одной правки данных (tests/test_sql_chain.py).
--
-- Колонки `payments` лежат и в теле CREATE TABLE в 001_init.sql — для чистой
-- базы; ALTER ниже — для живой, потому что `CREATE TABLE IF NOT EXISTS`
-- существующую таблицу не трогает. Каждая колонка из ALTER имеет часового в
-- infra/deploy.sh (`require_column`): без него недоехавший ALTER оставил бы
-- деплой зелёным, а бот — падающим на первом же платеже.

-- Кто платит, за какой счёт и что мы про платёж решили. Платёж пишется ДО того,
-- как что-либо ещё сделано с ним: журнал — единственный источник правды о деньгах.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS invoice_payload TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS kind TEXT CHECK (kind IN ('first', 'renewal', 'one_off', 'duplicate', 'unknown'));
ALTER TABLE payments ADD COLUMN IF NOT EXISTS is_first_recurring BOOLEAN NOT NULL DEFAULT FALSE;
-- `subscription_expiration_date` от Telegram: свою арифметику срока не заводим.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_end TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
-- SuccessfulPayment как пришёл: Telegram повторно ничего не пришлёт, а по этой
-- копии платёж можно разобрать вручную и вернуть.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS raw JSONB;

-- Согласие с условиями до покупки (требование Telegram к платным ботам) —
-- доказательство при споре: какая версия текста и когда. RESTRICT, а не CASCADE:
-- запись, нужная для спора, не должна исчезнуть вместе с клиентом.
CREATE TABLE IF NOT EXISTS user_consents (
    user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    doc         TEXT        NOT NULL,
    version     TEXT        NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, doc, version)
);

-- События оплаты без своего идентификатора платежа: апдейт `subscription`
-- (отмена, возврат, сбой продления), сообщение о возврате, обращение в поддержку.
-- `update_id` уникален только против повторной доставки; порядок событий по нему
-- определять нельзя (после недели тишины Telegram выбирает его случайно).
CREATE TABLE IF NOT EXISTS billing_events (
    id          BIGSERIAL PRIMARY KEY,
    update_id   BIGINT UNIQUE,
    kind        TEXT        NOT NULL,
    tg_user_id  BIGINT      NOT NULL,
    charge_id   TEXT,
    payload     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS billing_events_user_kind_idx
    ON billing_events (tg_user_id, kind, created_at DESC);
