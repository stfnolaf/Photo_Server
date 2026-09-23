ALTER TABLE image_fingerprints
    ADD COLUMN IF NOT EXISTS chroma_histogram VARCHAR(24);
