-- Обратимое исключение отслеживаемой группы из сбора по решению владельца.
--
-- Только идемпотентный DDL: цепочка прогоняется на КАЖДОМ деплое, правка данных здесь
-- запрещена (tests/test_sql_chain.py). Колонки лежат и в теле CREATE TABLE chats в
-- 001_init.sql (чистая база); ALTER ниже — для живой, у неё есть часовые в deploy.sh.
--
-- Исключённый чат остаётся строкой в `chats` (is_active=false, excluded_at задан): строка
-- нужна, чтобы разведка узнавала его по tg_id/username и не возвращала в очередь, а курсоры
-- last_msg_id/backfill_msg_id пережили restore. Из группы мы НЕ выходим и ничего в Telegram
-- не отправляем: исключение — запись в нашей базе, не действие аккаунта.
--
-- excluded_evidence — снимок доказательств на момент решения (период покрытия, число
-- сообщений, прошедших гейт, полезных предложений, backfill_done, курсоры, дата снимка).
-- Он лежит ОТДЕЛЬНО от raw_messages: сырьё без карточки чистится через 90 дней, и по
-- пропавшим строкам «ноль пользы» потом читался бы как «данных нет».
--
-- Номер 020: 019 занят повтором отказов (PR #26), PR #20 перенумеруется при мерже.
-- мержится позже, тот перенумеровывает файл, часового в deploy.sh и test_sql_numbers.py.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS excluded_at       TIMESTAMPTZ;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS excluded_reason   TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS excluded_evidence JSONB;

-- Журнал решений. Append-only: строки только добавляются, UPDATE/DELETE в коде нет.
-- tg_id без внешнего ключа: журнал обязан пережить удаление строки чата.
CREATE TABLE IF NOT EXISTS chat_exclusion_events (
    id        BIGSERIAL   PRIMARY KEY,
    tg_id     BIGINT      NOT NULL,
    action    TEXT        NOT NULL CHECK (action IN ('exclude', 'restore')),
    reason    TEXT        NOT NULL,
    evidence  JSONB,
    actor     TEXT        NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_exclusion_events_tg_idx ON chat_exclusion_events (tg_id, at);
