-- Reserve retained local-output capacity at job admission, across all dates.
-- Existing jobs start at zero; claim must acquire their budget before running.
-- The migration runner owns the transaction and checksum ledger.
ALTER TABLE replay_jobs
    ADD COLUMN output_budget_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN output_reserved_bytes BIGINT NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_replay_jobs_output_budget CHECK (output_budget_bytes >= 0),
    ADD CONSTRAINT ck_replay_jobs_output_reserved
        CHECK (output_reserved_bytes >= 0 AND output_reserved_bytes <= output_budget_bytes);
