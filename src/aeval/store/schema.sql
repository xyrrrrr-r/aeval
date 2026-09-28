-- aeval store schema. Raw data is permanent: nothing here is ever
-- overwritten — records are append-only, and artifact blobs live in
-- the content-addressed store, not in the DB.

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    manifest_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suite_runs (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    suite_id        TEXT NOT NULL,
    suite_version   TEXT NOT NULL,
    overlay_digest  TEXT NOT NULL,
    PRIMARY KEY (run_id, suite_id, suite_version, overlay_digest)
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    suite_id        TEXT NOT NULL,
    suite_version   TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    trial_index     INTEGER NOT NULL,
    stop_reason     TEXT NOT NULL,
    baseline_ok     INTEGER NOT NULL DEFAULT 1,
    requirements_json TEXT NOT NULL,
    observed_model_json TEXT,
    budget_json     TEXT,
    adapter_json    TEXT,
    claim_json      TEXT,
    artifacts_json  TEXT NOT NULL,
    transcript_extra_json TEXT,
    fork_json       TEXT,
    versions_json   TEXT,
    verdict         TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (run_id, suite_id, task_id, trial_index)
);

CREATE TABLE IF NOT EXISTS rubric_results (
    trial_id        TEXT NOT NULL REFERENCES trials(trial_id),
    grader_id       TEXT NOT NULL,
    grader_version  TEXT NOT NULL,
    layer           TEXT NOT NULL,
    veto            INTEGER NOT NULL DEFAULT 0,
    score_json      TEXT NOT NULL,
    status          TEXT NOT NULL,
    reasons_json    TEXT NOT NULL,
    coverage_json   TEXT,
    metrics_json    TEXT,
    produced_at     TEXT NOT NULL,
    PRIMARY KEY (trial_id, grader_id, grader_version)
);

CREATE TABLE IF NOT EXISTS completeness (
    trial_id        TEXT NOT NULL REFERENCES trials(trial_id),
    field           TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT,
    PRIMARY KEY (trial_id, field)
);

CREATE TABLE IF NOT EXISTS artifacts (
    sha256          TEXT PRIMARY KEY,
    media_type      TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    path            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(run_id);
CREATE INDEX IF NOT EXISTS idx_rubric_trial ON rubric_results(trial_id);
