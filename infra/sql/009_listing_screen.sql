-- ИИ-проверка карточки после бесплатного гейта (worker/screening.py).
--
-- `screened_at` пуст — карточку модель ещё не читала: новая, переписанная
-- кросспостом или накопленная до 18.09.2026. Проход берёт такие сам, поэтому
-- отдельной миграции-чистки нет: накопленный мусор гасится той же задачей, что
-- и свежий. `screen_note` — вердикт словами, чтобы отказ разбирался по базе.
ALTER TABLE listings ADD COLUMN IF NOT EXISTS screened_at TIMESTAMPTZ;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS screen_note TEXT;
CREATE INDEX IF NOT EXISTS listings_unscreened_idx
    ON listings (posted_at DESC)
    WHERE screened_at IS NULL AND is_active;
