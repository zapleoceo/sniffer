-- Deferred collection answers. Safe to rerun after 002_agent_catalog.sql.
ALTER TABLE collection_subscribers ADD COLUMN IF NOT EXISTS reply_queued_at TIMESTAMPTZ;

-- Здесь стоял UPDATE, возвращавший в очередь недавние задания старого воркера,
-- чей клиент ещё не получил ответ (релиз 18.09.2026). Из цепочки он убран:
-- деплой прогоняет каждый файл `infra/sql/00*.sql` на КАЖДОМ запуске, и
-- «разовая» правка данных срабатывала бы заново в течение суток после любого
-- следующего деплоя, давая упавшим задачам лишние попытки. Своё действие она
-- выполнила один раз; история — в git. Правило и его сторож:
-- tests/test_sql_chain.py.
