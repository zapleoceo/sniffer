-- Вкладки поиска: связь «поиск ↔ тема Telegram» и пометка «архив» (docs/search-tabs.md).
--
-- Только DDL: цепочка прогоняется на КАЖДОМ деплое, правка данных здесь запрещена
-- (tests/test_sql_chain.py). Новая таблица, а не колонка в `passports`: поиск — это
-- цепочка версий, и вторая сущность с тем же тождеством запрещена CLAUDE.md; здесь
-- лежит только то, чего в цепочке нет — тема, в которой поиск показан, и «убран».
--
-- Корень (`passport_root`) без внешнего ключа — как у `subscriptions.passport_root`:
-- корнем служит id первой версии, а NULL у неё в `root_id`.
--
-- `message_thread_id` пуст у поиска без темы: архив бывает и у человека, который тем не
-- видит. Уникальность пары «клиент, тема» не мешает таким строкам: NULL в UNIQUE не
-- сравниваются.
--
-- state: open — тема живая; lost — Telegram не нашёл тему (человек её удалил), доставка
-- идёт в General, а тема пересоздаётся при следующем обращении; archived — человек
-- убрал поиск (слежение на паузе, в списках его нет, версии сохранены).
CREATE TABLE IF NOT EXISTS search_tabs (
    id                BIGSERIAL   PRIMARY KEY,
    user_id           BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    passport_root     BIGINT      NOT NULL,
    message_thread_id BIGINT,
    shown_title       TEXT,
    state             TEXT        NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'lost', 'archived')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, passport_root),
    UNIQUE (user_id, message_thread_id)
);
