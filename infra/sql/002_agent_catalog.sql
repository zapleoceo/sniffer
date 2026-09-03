-- Additive collection queue. Apply after 001_init.sql; safe to rerun.
CREATE TABLE IF NOT EXISTS collection_tasks (
    id BIGSERIAL PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    scope JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ,
    result JSONB,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS collection_tasks_due_idx ON collection_tasks(status, run_after, id);
CREATE TABLE IF NOT EXISTS collection_subscribers (
    task_id BIGINT NOT NULL REFERENCES collection_tasks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    request_id BIGINT NOT NULL,
    request_version INTEGER NOT NULL CHECK (request_version > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY(task_id,user_id,request_id,request_version)
);
CREATE INDEX IF NOT EXISTS collection_subscribers_request_idx ON collection_subscribers(user_id,request_id);
CREATE TABLE IF NOT EXISTS collection_actions (
    task_id BIGINT NOT NULL REFERENCES collection_tasks(id) ON DELETE CASCADE,
    action_key TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(task_id,action_key)
);
