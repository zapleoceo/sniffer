-- Снимок публичного превью t.me/<username> и класс кандидата очереди вступлений.
--
-- Нужен ОБРАТИМОЙ приоритизации очереди (docs/chats-nha-trang.md, «Приоритет очереди»):
-- класс только двигает кандидата в порядке разбора, ничего не удаляет и не отклоняет.
-- preview_class: relevant | off_topic | foreign_city | unknown; NULL = превью не снималось
-- (приоритизация читает его как unknown). preview_evidence - совпавшие маркеры словами.
-- Данных не правим: цепочка идемпотентна и гоняется на каждом деплое.
-- Колонки есть и в теле CREATE TABLE chat_candidates в 001_init.sql; ALTER - для живой базы.
ALTER TABLE chat_candidates ADD COLUMN IF NOT EXISTS preview_class      TEXT;
ALTER TABLE chat_candidates ADD COLUMN IF NOT EXISTS preview_evidence   TEXT;
ALTER TABLE chat_candidates ADD COLUMN IF NOT EXISTS preview_snapshot   JSONB;
ALTER TABLE chat_candidates ADD COLUMN IF NOT EXISTS preview_checked_at TIMESTAMPTZ;
