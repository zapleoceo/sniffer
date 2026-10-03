-- Журнал показанных карточек и периоды квоты (docs/monetization.md).
--
-- Единица квоты — уникальная карточка за период на аккаунт: повтор в периоде
-- бесплатен, в новом периоде карточка снова платная. Период — «годовщина» от
-- якоря: якорь + k календарных месяцев по ВЬЕТНАМСКОМУ календарю (UTC+7), каждая
-- граница от якоря, а не от прошлой границы (иначе обрезка 31-го копилась бы).
-- Границы хранятся как timestamptz и считаются в UTC.
--
-- Только DDL: цепочка прогоняется на КАЖДОМ деплое, и править данные здесь нельзя
-- (tests/test_sql_chain.py). Поэтому ни триггера со счётчиком, ни UPDATE: число
-- занятых карточек — count(*) под блокировкой строки периода (SELECT ... FOR
-- UPDATE), а последний барьер — уникальность (period_id, listing_id).

-- Якорь: момент первого списания квоты. Ставится один раз и после появления
-- периодов неизменяем — составной внешний ключ ниже не даст его поменять.
ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_anchor_at TIMESTAMPTZ;
-- Когда человеку в последний раз предложили подписку: не чаще раза в сутки.
ALTER TABLE users ADD COLUMN IF NOT EXISTS paywall_offered_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS users_id_anchor_uidx ON users (id, quota_anchor_at);

-- Явные периоды: создаются лениво при первом списании в периоде. CHECK требует,
-- чтобы границы совпадали с формулой, поэтому ошибка в арифметике приложения не
-- запишет «не ту» границу, а уронит вставку. CHECK в ОДНУ строку: парсер
-- tests/test_db_models.py читает первое слово каждой строки как имя колонки.
CREATE TABLE IF NOT EXISTS quota_periods (
    id           BIGSERIAL   PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anchor_at    TIMESTAMPTZ NOT NULL,
    period_no    INT         NOT NULL CHECK (period_no >= 0),
    period_start TIMESTAMPTZ NOT NULL,
    period_end   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (period_start = ((anchor_at AT TIME ZONE 'Asia/Ho_Chi_Minh') + period_no * interval '1 month') AT TIME ZONE 'Asia/Ho_Chi_Minh' AND period_end = ((anchor_at AT TIME ZONE 'Asia/Ho_Chi_Minh') + (period_no + 1) * interval '1 month') AT TIME ZONE 'Asia/Ho_Chi_Minh'),
    UNIQUE (user_id, period_no),
    UNIQUE (id, user_id),
    FOREIGN KEY (user_id, anchor_at) REFERENCES users (id, quota_anchor_at)
);

-- Журнал показов: одна строка — одна карточка в одном периоде. Без CASCADE на
-- listings: карточки не удаляются, а квота не должна «возвращаться» молча.
-- delivered_at пуст, пока Telegram не принял сообщение: это резерв, и зависшие
-- резервы снимает QuotaRepository.sweep_stale.
CREATE TABLE IF NOT EXISTS offer_views (
    id            BIGSERIAL   PRIMARY KEY,
    user_id       BIGINT      NOT NULL,
    period_id     BIGINT      NOT NULL,
    listing_id    BIGINT      NOT NULL REFERENCES listings(id),
    passport_root BIGINT      REFERENCES passports(id) ON DELETE SET NULL,
    request_id    BIGINT      REFERENCES client_requests(id) ON DELETE SET NULL,
    channel       TEXT        NOT NULL CHECK (channel IN ('search', 'deferred', 'monitor')),
    times_shown   INT         NOT NULL DEFAULT 1,
    shown_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at  TIMESTAMPTZ,
    UNIQUE (period_id, listing_id),
    FOREIGN KEY (period_id, user_id) REFERENCES quota_periods (id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS offer_views_user_listing_idx ON offer_views (user_id, listing_id);
CREATE INDEX IF NOT EXISTS offer_views_unconfirmed_idx ON offer_views (shown_at) WHERE delivered_at IS NULL;

-- Сколько карточек реально показано и сколько удержано лимитом: спрос на подписку
-- виден в самом журнале запросов, а не восстанавливается по тексту ответов.
ALTER TABLE client_requests ADD COLUMN IF NOT EXISTS shown_count INT NOT NULL DEFAULT 0;
ALTER TABLE client_requests ADD COLUMN IF NOT EXISTS withheld_count INT NOT NULL DEFAULT 0;
