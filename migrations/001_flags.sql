-- Flag configuration, snapshot versioning, and the audit trail.
--
-- Shape of this schema: the fields an operator queries and constrains at the
-- database level -- status and rollout percentage -- are real columns with real
-- CHECK constraints. The rest of the definition (variants, targeting, quality
-- policy, rollout plan) is a JSONB document, because it is always read as a
-- whole and never queried field-by-field. Normalising it would mean six more
-- tables and a join for every snapshot publish, to support queries nothing
-- issues.
--
-- Status and percentage are deliberately NOT duplicated inside the JSONB
-- document. Two copies of the same fact drift, and the copy the data plane reads
-- would be the one that goes stale.

CREATE TABLE IF NOT EXISTS flags (
    key                TEXT PRIMARY KEY,
    status             TEXT NOT NULL
                       CHECK (status IN ('off', 'shadow', 'rolling_out',
                                         'paused', 'fully_on', 'rolled_back')),
    rollout_percentage DOUBLE PRECISION NOT NULL
                       CHECK (rollout_percentage >= 0 AND rollout_percentage <= 100),
    definition         JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A single monotonic counter across all flags. One counter, rather than a
-- per-flag version, is what lets the SDK reject an out-of-order snapshot with a
-- comparison instead of a merge.
CREATE TABLE IF NOT EXISTS snapshot_version (
    only_row BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (only_row),
    version  BIGINT NOT NULL DEFAULT 0
);

INSERT INTO snapshot_version (only_row, version)
VALUES (TRUE, 0)
ON CONFLICT (only_row) DO NOTHING;

-- Append-only. No UPDATE or DELETE is issued against this table anywhere in the
-- codebase: a rollback record that can be edited afterwards is not an audit
-- trail. snapshot_version ties each entry to the exact configuration the data
-- plane served immediately after the change.
CREATE TABLE IF NOT EXISTS audit_events (
    id               BIGSERIAL PRIMARY KEY,
    flag_key         TEXT NOT NULL,
    action           TEXT NOT NULL,
    actor            TEXT NOT NULL CHECK (length(btrim(actor)) > 0),
    reason           TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
    at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot_version BIGINT NOT NULL,
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_events_flag_key_idx
    ON audit_events (flag_key, id);
