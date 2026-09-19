-- Per-user PC download tickets. Original URLs are encrypted; tokens are hashed.
CREATE TABLE IF NOT EXISTS replay_device_imports (
    id VARCHAR(32) PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    subject VARCHAR(200) NOT NULL,
    name VARCHAR(180) NOT NULL,
    source_ciphertext TEXT NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL,
    max_bytes INTEGER NOT NULL,
    max_duration INTEGER NOT NULL,
    bytes INTEGER,
    sha256 VARCHAR(64),
    expires_at DOUBLE PRECISION NOT NULL,
    error_code VARCHAR(80)
);
CREATE INDEX IF NOT EXISTS ix_replay_device_imports_owner ON replay_device_imports (tenant_id, subject, state);
CREATE INDEX IF NOT EXISTS ix_replay_device_imports_expiry ON replay_device_imports (expires_at);
