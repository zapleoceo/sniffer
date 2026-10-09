-- Курсор серверной доставки кандидатов слежения в комнату агентов (notifier/room_relay.py).
--
-- Только DDL: цепочка прогоняется на КАЖДОМ деплое, правка данных здесь запрещена
-- (tests/test_sql_chain.py). Таблица, а не колонка в `notifications`: курсор принадлежит
-- получателю (комнате), а не уведомлению, и у второго получателя будет свой.
--
-- Один ряд на подписку: `last_notification_id` — наибольший id уведомления, которое комната
-- ПРИНЯЛА. Двигается только после успешного room_post; нет ряда — ничего не доставлено, и
-- ретрансляция начнёт с первого уведомления (повтор безвреден: message_id детерминирован,
-- сервер комнаты отвечает deduped).
CREATE TABLE IF NOT EXISTS room_relay_cursor (
    subscription_id      BIGINT      PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
    last_notification_id BIGINT      NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
