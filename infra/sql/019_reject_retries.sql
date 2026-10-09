-- Адресный повтор временного отказа из дашборда (docs/dashboard.md, раздел «Повтор отказа»).
--
-- Только идемпотентный DDL: цепочка прогоняется на КАЖДОМ деплое, правка данных здесь
-- запрещена (tests/test_sql_chain.py). Таблица новая и в 001_init.sql не дублируется —
-- как `search_tabs` в 017; у неё есть часовой в infra/deploy.sh.
--
-- Журнал попыток, а не флаг на отказе. Строка `chat_rejects` при повторе переезжает в
-- очередь `chat_candidates` и из `chat_rejects` исчезает (иначе новый отказ того же ключа
-- не записался бы: `reject()` делает ON CONFLICT DO NOTHING и оставил бы старое время).
-- Чтобы аудит не терялся, снимок исходного отказа (причина и время) копируется СЮДА в той
-- же транзакции, что и перенос. Ссылка на отказ — `reject_key` плюс снимок; внешнего
-- ключа нет сознательно: отказ перестаёт существовать как строка, а попытка обязана жить.
--
-- idempotency_key — токен формы: повторный POST той же формы возвращает ту же попытку,
-- а не заводит вторую. Частичный уникальный индекс по `reject_key` WHERE status='active'
-- не даёт двух одновременно живых попыток на один ключ при ЛЮБЫХ токенах.
-- status: active — ключ ещё в очереди кандидатов; done — очередь его отпустила (исход в
-- `outcome`: left_queue — вступили или сняли, rejected_again — отклонён снова).
-- next_retry_at — раньше этого момента следующую попытку на ключ не принимаем (cooldown).
CREATE TABLE IF NOT EXISTS chat_reject_retries (
    id                 BIGSERIAL   PRIMARY KEY,
    reject_key         TEXT        NOT NULL,
    reject_reason      TEXT        NOT NULL,
    reject_rejected_at TIMESTAMPTZ,
    requested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by       BIGINT      NOT NULL,
    idempotency_key    TEXT        NOT NULL UNIQUE,
    status             TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
    outcome            TEXT,
    settled_at         TIMESTAMPTZ,
    next_retry_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS chat_reject_retries_key_idx
    ON chat_reject_retries (reject_key, requested_at DESC);
CREATE INDEX IF NOT EXISTS chat_reject_retries_requested_idx
    ON chat_reject_retries (requested_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS chat_reject_retries_one_active_idx
    ON chat_reject_retries (reject_key) WHERE status = 'active';
