-- Additive verified catalog. Existing listings are deliberately NOT backfilled as verified.
CREATE TABLE IF NOT EXISTS catalog_observations (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL REFERENCES collection_tasks(id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (task_id, source, external_id, content_hash)
);
CREATE TABLE IF NOT EXISTS catalog_publications (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    observation_id BIGINT NOT NULL REFERENCES catalog_observations(id),
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    deal_type TEXT NOT NULL,
    price_vnd BIGINT,
    active BOOLEAN NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS catalog_publications_search_idx
    ON catalog_publications(city, category, deal_type, fetched_at DESC) WHERE active;
CREATE TABLE IF NOT EXISTS catalog_coverage (
    scope_key TEXT NOT NULL,
    source TEXT NOT NULL,
    task_id BIGINT NOT NULL REFERENCES collection_tasks(id),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'error', 'unsupported')),
    PRIMARY KEY (scope_key, source)
);
