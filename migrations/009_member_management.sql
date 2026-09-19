-- Audit administrator actions without retaining email, tokens or stream keys.
CREATE TABLE IF NOT EXISTS replay_member_audit (
    id VARCHAR(64) PRIMARY KEY,
    actor_hash VARCHAR(64) NOT NULL,
    member_id VARCHAR(128) NOT NULL,
    action VARCHAR(32) NOT NULL,
    target VARCHAR(24) NOT NULL,
    at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_member_audit_member_at ON replay_member_audit(member_id, at);
