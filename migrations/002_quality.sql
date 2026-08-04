-- Quality observations and rollout progress.
--
-- quality_samples is the durable record every rollout decision is made from.
-- It is deliberately the source of truth rather than a Redis counter: a decision
-- to roll a feature back for users must be justifiable from data that cannot be
-- evicted under memory pressure, and must still be reconstructable months later
-- when someone asks why the rollout stopped.
--
-- `scored` is the column that keeps a broken judge visible. An unscored sample
-- still occupies a row -- it happened, it was served to someone -- but carries no
-- value. Averaging it in as zero would invent a regression; dropping the row
-- would let a blind rollout look like a quiet one.

CREATE TABLE IF NOT EXISTS quality_samples (
    id            BIGSERIAL PRIMARY KEY,
    flag_key      TEXT NOT NULL,
    evaluation_id TEXT NOT NULL,
    variant_kind  TEXT NOT NULL CHECK (variant_kind IN ('baseline', 'experimental')),
    signal        TEXT NOT NULL
                  CHECK (signal IN ('judge_score', 'feedback', 'latency_ms',
                                    'error_rate', 'unscored_rate')),
    value         DOUBLE PRECISION,
    scored        BOOLEAN NOT NULL,
    is_shadow     BOOLEAN NOT NULL DEFAULT FALSE,
    reason        TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An unscored sample must not carry a value, mirroring JudgeVerdict's own
    -- invariant. Enforced here too, because this table outlives the process that
    -- wrote it and is read directly by analytics.
    CONSTRAINT scored_samples_have_values
        CHECK ((scored AND value IS NOT NULL) OR (NOT scored AND value IS NULL))
);

-- The controller's read pattern: newest samples for one flag, variant and
-- signal, optionally excluding shadow traffic.
CREATE INDEX IF NOT EXISTS quality_samples_window_idx
    ON quality_samples (flag_key, signal, variant_kind, is_shadow, id DESC);

-- One row per flag, tracking where its staged rollout has reached.
--
-- stage_entered_at is what dwell time is measured against, so it is reset only
-- when the stage actually changes -- not on every controller tick, which would
-- make a stage never mature.
CREATE TABLE IF NOT EXISTS rollout_state (
    flag_key         TEXT PRIMARY KEY,
    stage_index      INTEGER NOT NULL DEFAULT 0 CHECK (stage_index >= 0),
    stage_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at   TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every controller decision, including the ones that changed nothing.
--
-- Holds are recorded as well as actions: "why did this rollout sit at 5% for six
-- hours" is the question an operator actually asks, and it is unanswerable from
-- a log that only records changes.
CREATE TABLE IF NOT EXISTS controller_decisions (
    id           BIGSERIAL PRIMARY KEY,
    flag_key     TEXT NOT NULL,
    action       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
    canary       JSONB,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS controller_decisions_flag_idx
    ON controller_decisions (flag_key, id DESC);
