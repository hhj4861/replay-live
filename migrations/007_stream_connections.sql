-- Per-account destinations. Both the server URL and key are encrypted together.
-- The subject digest is part of the authenticated encryption context, alongside
-- the tenant, target and record purpose; copying a row cannot change its owner.
CREATE TABLE replay_stream_connections (
    tenant_id VARCHAR(200) NOT NULL,
    subject_hash VARCHAR(64) NOT NULL,
    target VARCHAR(24) NOT NULL,
    secret_ciphertext TEXT NOT NULL,
    updated_at FLOAT NOT NULL,
    PRIMARY KEY (tenant_id, subject_hash, target),
    CONSTRAINT replay_stream_connections_live_target
        CHECK (target IN ('youtube','twitch','facebook','instagram','tiktok','naver','chzzk','kick','custom'))
);
