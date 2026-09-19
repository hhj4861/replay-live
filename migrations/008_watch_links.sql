-- Viewing links are user-managed history metadata, never worker destinations.
-- Existing history intentionally has no guessed external URL.
ALTER TABLE replay_jobs
    ADD COLUMN channel_url VARCHAR(2048) NOT NULL DEFAULT '',
    ADD COLUMN broadcast_url VARCHAR(2048) NOT NULL DEFAULT '';
