-- Recurring broadcasts and owner-authorized YouTube channel connections.

CREATE TABLE replay_automation_runs (
	id VARCHAR(32) NOT NULL,
	schedule_id VARCHAR(32) NOT NULL,
	scheduled_for FLOAT NOT NULL,
	state VARCHAR(24) NOT NULL,
	video_id VARCHAR(64),
	media_id VARCHAR(64),
	import_job_id VARCHAR(64),
	jobs_json TEXT NOT NULL,
	error_code VARCHAR(80),
	created FLOAT NOT NULL,
	updated FLOAT NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_replay_automation_runs_schedule_id ON replay_automation_runs (schedule_id);

CREATE TABLE replay_automations (
	id VARCHAR(32) NOT NULL,
	tenant_id VARCHAR(200) NOT NULL,
	subject VARCHAR(4096) NOT NULL,
	subject_hash VARCHAR(64) NOT NULL,
	name VARCHAR(180) NOT NULL,
	config_json TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	deleted BOOLEAN NOT NULL,
	next_run FLOAT NOT NULL,
	active_run VARCHAR(32),
	last_video_id VARCHAR(64),
	lease_token VARCHAR(32),
	lease_until FLOAT NOT NULL,
	created FLOAT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE replay_youtube_channels (
	tenant_id VARCHAR(200) NOT NULL,
	subject_hash VARCHAR(64) NOT NULL,
	channel_id VARCHAR(64) NOT NULL,
	title VARCHAR(180) NOT NULL,
	uploads_id VARCHAR(80) NOT NULL,
	secret_ciphertext TEXT NOT NULL,
	PRIMARY KEY (tenant_id, subject_hash)
);

CREATE TABLE replay_youtube_oauth_flows (
	state_hash VARCHAR(64) NOT NULL,
	tenant_id VARCHAR(200) NOT NULL,
	subject_hash VARCHAR(64) NOT NULL,
	secret_ciphertext TEXT NOT NULL,
	expires_at FLOAT NOT NULL,
	PRIMARY KEY (state_hash)
);
