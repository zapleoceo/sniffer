-- Когда и почему карточка снята с выдачи.
--
-- До сих пор `is_active=false` не говорил ничего: возраст, ИИ-проверка, перечитывание
-- чата и обход Chotot гасили одинаково, и по базе нельзя было понять, какой из путей снимает
-- лишнее. Причины: expired (возраст), screen (ИИ-проверка), liveness_deleted (пост удалён),
-- liveness_closed (исправлен в «продано»), chotot_unseen (нет на доске), superseded (карточку
-- заменила новая после смены направления/категории).
--
-- Старые записи НЕ размечаются задним числом: NULL = неизвестно, а не «не снята». Данных
-- здесь не правим (цепочка идемпотентна и гоняется на каждом деплое, tests/test_sql_chain.py).
-- Колонки есть и в теле CREATE TABLE listings в 001_init.sql; ALTER ниже - для живой базы.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS deactivated_at     TIMESTAMPTZ;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS deactivated_reason TEXT;
