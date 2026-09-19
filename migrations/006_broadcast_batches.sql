-- A batch key outlives job retention so partial history cannot recreate jobs.
-- Group admission shares the repository's global quota/admission transaction.
CREATE TABLE replay_broadcast_batches (
    tenant_id VARCHAR(200) NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    job_ids_json TEXT NOT NULL,
    created FLOAT NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);
