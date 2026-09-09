-- Exact named requests are filtered before LIMIT.  Facts provide cheap equality;
-- grounded source text is the recall fallback when extraction omitted a name.
CREATE INDEX IF NOT EXISTS catalog_observations_brand_idx
    ON catalog_observations (lower(payload->'facts'->>'brand'));
CREATE INDEX IF NOT EXISTS catalog_observations_model_idx
    ON catalog_observations (lower(payload->'facts'->>'model'));
CREATE INDEX IF NOT EXISTS catalog_observations_text_trgm_idx
    ON catalog_observations USING GIN (
        lower((payload->>'title') || ' ' || (payload->>'raw_text')) gin_trgm_ops
    );
