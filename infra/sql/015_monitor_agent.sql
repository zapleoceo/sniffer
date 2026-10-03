-- Монитор по слоту (worker/monitor.py, docs/architecture.md 7.2): суточный потолок
-- 10 по вьетнамским суткам, сводка «ещё N» сверх потолка, пауза без слота.
--
-- Только DDL: цепочка прогоняется на КАЖДОМ деплое, и правка данных здесь исполнялась бы
-- после каждого следующего (tests/test_sql_chain.py). У всех колонок состояние «ничего
-- не было» — DEFAULT 0, FALSE и NULL, поэтому существующим строкам ничего делать не надо.
--
-- Потолок слота в сутки: 10. Меняется ТОЛЬКО умолчание для новых подписок; у существующих
-- остаётся то, что у них записано (подписок на 04.10.2026 нет, а чужое число не наше дело).
ALTER TABLE subscriptions ALTER COLUMN max_per_day SET DEFAULT 10;

-- Сколько карточек потолок отбросил за всё время слота: «сужайте запрос» держится на числе.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS suppressed_total INT NOT NULL DEFAULT 0;
-- Сводка «ещё N»: сутки (вьетнамские), к которым относится счёт, число отброшенных в них и
-- признак, что о них клиенту уже написано. Сутки сменились — счёт начинается заново.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS overflow_day DATE;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS overflow_count INT NOT NULL DEFAULT 0;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS overflow_notified BOOLEAN NOT NULL DEFAULT FALSE;
-- Когда слот остался без права (подписок Stars меньше, чем слежений). Пусто — слот работает.
-- Нужен, чтобы после долгой паузы курсор прыгнул к «сейчас», а не вываливал пачку.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS no_slot_since TIMESTAMPTZ;
