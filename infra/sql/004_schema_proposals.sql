-- Advisory changes only: no SQL/function bodies or automatic execution.
CREATE TABLE IF NOT EXISTS schema_proposals (
    id BIGSERIAL PRIMARY KEY,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('request', 'task')),
    owner_id BIGINT NOT NULL,
    content_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review' CHECK (status IN ('pending_review','accepted','rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (owner_kind, owner_id, content_hash)
);
