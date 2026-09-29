CREATE TABLE replay_youtube_grants (
    tenant_id VARCHAR(200) NOT NULL,
    subject_hash VARCHAR(64) NOT NULL,
    scopes TEXT NOT NULL,
    PRIMARY KEY (tenant_id, subject_hash)
);
CREATE TABLE replay_youtube_broadcasts (
    run_id VARCHAR(32) PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    subject_hash VARCHAR(64) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    stream_id VARCHAR(128) NOT NULL,
    broadcast_id VARCHAR(64),
    phase VARCHAR(32) NOT NULL,
    live_seen BOOLEAN NOT NULL,
    created FLOAT NOT NULL,
    updated FLOAT NOT NULL
);
