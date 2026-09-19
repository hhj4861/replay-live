-- Google identities persist through restore; challenges and app sessions do not.
CREATE TABLE IF NOT EXISTS replay_google_identities (
    subject VARCHAR(255) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL UNIQUE,
    email VARCHAR(254) NOT NULL,
    email_authoritative BOOLEAN NOT NULL,
    roles VARCHAR(500) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_google_challenges (
    id VARCHAR(64) PRIMARY KEY,
    nonce_hash VARCHAR(64) NOT NULL,
    secret_hash VARCHAR(64) NOT NULL,
    client_hash VARCHAR(64) NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_google_challenges_expires_at ON replay_google_challenges(expires_at);
CREATE INDEX IF NOT EXISTS ix_replay_google_challenges_client_created ON replay_google_challenges(client_hash, created_at);
CREATE TABLE IF NOT EXISTS replay_google_session_families (
    id VARCHAR(64) PRIMARY KEY,
    subject VARCHAR(255) NOT NULL REFERENCES replay_google_identities(subject) ON DELETE CASCADE,
    absolute_expires_at DOUBLE PRECISION NOT NULL,
    revoked BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_google_session_families_subject ON replay_google_session_families(subject);
CREATE INDEX IF NOT EXISTS ix_replay_google_session_families_absolute_expires_at ON replay_google_session_families(absolute_expires_at);
CREATE TABLE IF NOT EXISTS replay_google_sessions (
    token_hash VARCHAR(64) PRIMARY KEY,
    family_id VARCHAR(64) NOT NULL REFERENCES replay_google_session_families(id) ON DELETE CASCADE,
    retired BOOLEAN NOT NULL,
    subject VARCHAR(255) NOT NULL REFERENCES replay_google_identities(subject) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    absolute_expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_google_sessions_family_id ON replay_google_sessions(family_id);
CREATE INDEX IF NOT EXISTS ix_replay_google_sessions_subject ON replay_google_sessions(subject);
CREATE INDEX IF NOT EXISTS ix_replay_google_sessions_expires_at ON replay_google_sessions(expires_at);
