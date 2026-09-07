-- Verified catalog publications feed the existing matcher/notifier pipeline.
ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS catalog_observation_id BIGINT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='listings_catalog_observation_fk'
    ) THEN
        ALTER TABLE listings ADD CONSTRAINT listings_catalog_observation_fk
            FOREIGN KEY (catalog_observation_id) REFERENCES catalog_observations(id)
            ON DELETE SET NULL;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS listings_catalog_observation_idx
    ON listings(catalog_observation_id) WHERE catalog_observation_id IS NOT NULL;
