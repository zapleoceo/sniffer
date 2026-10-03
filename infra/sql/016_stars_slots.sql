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
-- клиента. Ограничение пересоздаётся ТОЛЬКО если оно ещё не CASCADE: DROP + ADD берут
-- блокировку ACCESS EXCLUSIVE на users на каждом деплое, а запуск, которому менять
-- нечего, не должен ни ждать чужие транзакции, ни ставить их в очередь за собой.
-- lock_timeout не даёт деплою зависнуть в очереди за долгой транзакцией: он падает
-- (и деплой красный), а не блокирует бота. Данные не трогаются.
DO $fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_consents_user_id_fkey'
          AND conrelid = 'user_consents'::regclass
          AND confdeltype = 'c'
    ) THEN
        SET LOCAL lock_timeout = '5s';
        ALTER TABLE user_consents DROP CONSTRAINT IF EXISTS user_consents_user_id_fkey;
        ALTER TABLE user_consents
            ADD CONSTRAINT user_consents_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    END IF;
END
$fk$;

-- Сверка спрашивает «есть ли уже такое событие по этому платежу»: без индекса — полный обход.
CREATE INDEX IF NOT EXISTS billing_events_charge_idx ON billing_events (kind, charge_id);
