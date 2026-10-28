-- News progress is independent from the marketplace's raw_messages.stage.
-- No rows are populated and no news consumer is enabled by this migration.
CREATE TABLE IF NOT EXISTS news_processing (
    raw_message_id BIGINT PRIMARY KEY REFERENCES raw_messages(id) ON DELETE RESTRICT,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'reviewed', 'rejected')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
