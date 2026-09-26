-- Shared provider balance, without tenant identifiers or credentials.
CREATE TABLE IF NOT EXISTS replay_proxy_balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    remaining_bytes BIGINT NOT NULL CHECK (remaining_bytes >= 0),
    observed_at DOUBLE PRECISION NOT NULL,
    low_fired BOOLEAN NOT NULL,
    critical_fired BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_proxy_alerts (
    id VARCHAR(32) PRIMARY KEY,
    threshold_bytes BIGINT NOT NULL,
    remaining_bytes BIGINT NOT NULL CHECK (remaining_bytes >= 0),
    created_at DOUBLE PRECISION NOT NULL,
    lease_token VARCHAR(32),
    lease_until DOUBLE PRECISION NOT NULL,
    acknowledged_at DOUBLE PRECISION
);
