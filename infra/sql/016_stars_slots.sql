-- Слоты мониторинга из подписок Stars: порядок претензии на слот, источник платежа,
-- политика удаления клиента.
--
-- Деплой гонит каждый файл цепочки на КАЖДОМ запуске: только идемпотентный DDL, ни одной
-- правки данных (tests/test_sql_chain.py). Колонки лежат и в теле CREATE TABLE в
-- 001_init.sql (чистая база); ALTER ниже — для живой, у каждой есть часовой в deploy.sh.

-- Порядок претензии на слот: слоты берут первые по (priority, id), остальные мониторинги
-- клиента ждут (⌛), но ничего не теряют. Перенос слота на другой поиск — смена порядка.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 0;

-- Откуда запись о платеже: апдейт Telegram или сверка по истории звёзд, и честная ли дата
-- окончания. Срок, посчитанный сверкой (дата платежа + период), — оценка, а не факт.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'update';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS period_end_estimated BOOLEAN NOT NULL DEFAULT FALSE;

-- Единая политика удаления клиента: всё, что про него, уходит вместе с ним (CASCADE) —
-- как у платежей и подписок. До этого согласие держало RESTRICT и блокировало удаление
-- клиента. DROP + ADD идемпотентны: ограничение пересоздаётся тем же, а данные не трогаются.
ALTER TABLE user_consents DROP CONSTRAINT IF EXISTS user_consents_user_id_fkey;
ALTER TABLE user_consents
    ADD CONSTRAINT user_consents_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Сверка спрашивает «есть ли уже такое событие по этому платежу»: без индекса — полный обход.
CREATE INDEX IF NOT EXISTS billing_events_charge_idx ON billing_events (kind, charge_id);
