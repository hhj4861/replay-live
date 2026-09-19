-- Replay Live commercial schema v1. The migration runner owns transaction, lock and checksum ledger.

-- Application requests must use tenant predicates; worker callbacks use fenced leases.

-- Object deletion outbox retains quota until signed uploads expire and removal is acknowledged.

CREATE TABLE IF NOT EXISTS replay_coordination (
	id SERIAL NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS replay_daily_usage (
	tenant_id VARCHAR(200) NOT NULL, 
	day INTEGER NOT NULL, 
	jobs INTEGER NOT NULL, 
	reserved_seconds FLOAT NOT NULL, 
	PRIMARY KEY (tenant_id, day)
);

CREATE TABLE IF NOT EXISTS replay_idempotency (
	tenant_id VARCHAR(200) NOT NULL, 
	idempotency_key VARCHAR(200) NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	created FLOAT NOT NULL, 
	PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS replay_media (
	id VARCHAR(64) NOT NULL, 
	tenant_id VARCHAR(200) NOT NULL, 
	name VARCHAR(180) NOT NULL, 
	object_key VARCHAR(1024) NOT NULL, 
	bytes BIGINT NOT NULL, 
	duration FLOAT NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	fps FLOAT NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	etag VARCHAR(200) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	error_code VARCHAR(80), 
	created FLOAT NOT NULL, 
	updated FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_replay_media_tenant_id UNIQUE (tenant_id, id), 
	UNIQUE (object_key)
);

CREATE TABLE IF NOT EXISTS replay_object_deletions (
	id VARCHAR(64) NOT NULL, 
	tenant_id VARCHAR(200) NOT NULL, 
	object_key VARCHAR(1024) NOT NULL, 
	created FLOAT NOT NULL, 
	not_before FLOAT NOT NULL, 
	attempt INTEGER NOT NULL, 
	error_code VARCHAR(80), 
	PRIMARY KEY (id), 
	UNIQUE (object_key)
);

CREATE TABLE IF NOT EXISTS replay_output_reservations (
	object_key VARCHAR(1024) NOT NULL, 
	tenant_id VARCHAR(200) NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	lease_version INTEGER NOT NULL, 
	bytes BIGINT NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	expires_at FLOAT NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	PRIMARY KEY (object_key)
);

CREATE TABLE IF NOT EXISTS replay_runtimes (
	job_id VARCHAR(64) NOT NULL, 
	lease_version INTEGER NOT NULL, 
	state VARCHAR(24) NOT NULL, 
	deadline FLOAT NOT NULL, 
	updated FLOAT NOT NULL, 
	cleaned BOOLEAN NOT NULL, 
	PRIMARY KEY (job_id, lease_version)
);

CREATE TABLE IF NOT EXISTS replay_workers (
	id VARCHAR(200) NOT NULL, 
	last_seen FLOAT NOT NULL, 
	metadata_json TEXT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS replay_jobs (
	id VARCHAR(64) NOT NULL, 
	tenant_id VARCHAR(200) NOT NULL, 
	media_id VARCHAR(64) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	target VARCHAR(24) NOT NULL, 
	secret_ciphertext TEXT, 
	idempotency_key VARCHAR(200) NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	scheduled FLOAT NOT NULL, 
	reserved_until FLOAT NOT NULL, 
	deadline FLOAT NOT NULL, 
	state VARCHAR(24) NOT NULL, 
	progress FLOAT NOT NULL, 
	attempt INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	next_run FLOAT NOT NULL, 
	lease_token VARCHAR(64), 
	lease_version INTEGER NOT NULL, 
	lease_expires FLOAT, 
	worker_id VARCHAR(200), 
	cancel_requested BOOLEAN NOT NULL, 
	output_key VARCHAR(1024), 
	output_bytes BIGINT NOT NULL, 
	created FLOAT NOT NULL, 
	updated FLOAT NOT NULL, 
	error_code VARCHAR(80), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_replay_jobs_idempotency UNIQUE (tenant_id, idempotency_key), 
	CONSTRAINT uq_replay_jobs_tenant_id UNIQUE (tenant_id, id), 
	FOREIGN KEY(tenant_id, media_id) REFERENCES replay_media (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS replay_events (
	id SERIAL NOT NULL, 
	tenant_id VARCHAR(200) NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	at FLOAT NOT NULL, 
	code VARCHAR(80) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id, job_id) REFERENCES replay_jobs (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_replay_media_tenant_created ON replay_media (tenant_id, created);

CREATE INDEX IF NOT EXISTS ix_replay_jobs_due ON replay_jobs (state, next_run);

CREATE INDEX IF NOT EXISTS ix_replay_jobs_lease ON replay_jobs (state, lease_expires);

CREATE INDEX IF NOT EXISTS ix_replay_jobs_tenant_created ON replay_jobs (tenant_id, created);

CREATE INDEX IF NOT EXISTS ix_replay_events_job_at ON replay_events (job_id, at);

INSERT INTO replay_coordination (id) VALUES (1) ON CONFLICT DO NOTHING;
