-- Aggregate-only alert outbox. Notification delivery is a separate, trusted task.
CREATE TABLE IF NOT EXISTS replay_alert_outbox (
    id VARCHAR(32) PRIMARY KEY,
    code VARCHAR(80) NOT NULL,
    count INTEGER NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    acknowledged_at DOUBLE PRECISION,
    CONSTRAINT ck_replay_alert_count CHECK (count >= 1)
);
CREATE TABLE IF NOT EXISTS replay_alert_cooldowns (
    code VARCHAR(80) PRIMARY KEY,
    next_emit_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_alert_outbox_pending
    ON replay_alert_outbox (acknowledged_at, created);
CREATE UNIQUE INDEX IF NOT EXISTS uq_replay_pending_alert_code
    ON replay_alert_outbox (code) WHERE acknowledged_at IS NULL;
