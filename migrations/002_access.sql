-- Release 002: shared access budgets and revocation. Immutable after apply.

CREATE TABLE replay_access_counters (
	bucket VARCHAR(64) NOT NULL, 
	count INTEGER NOT NULL, 
	reset_at FLOAT NOT NULL, 
	PRIMARY KEY (bucket)
);

CREATE INDEX ix_replay_access_counters_reset_at ON replay_access_counters (reset_at);

CREATE TABLE replay_revoked_tokens (
	token_hash VARCHAR(64) NOT NULL, 
	expires_at FLOAT NOT NULL, 
	PRIMARY KEY (token_hash)
);

CREATE INDEX ix_replay_revoked_tokens_expires_at ON replay_revoked_tokens (expires_at);
