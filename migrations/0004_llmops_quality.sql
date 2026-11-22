-- Quality: feedback -> dataset curation, offline/online evaluation results, drift reports.

CREATE TABLE IF NOT EXISTS eval_cases (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    topic              VARCHAR(64)  NOT NULL,
    source_request_id  VARCHAR(128) NULL,
    prompt             MEDIUMTEXT   NOT NULL,
    answer             MEDIUMTEXT   NULL,
    label              VARCHAR(16)  NOT NULL,                     -- positive|negative
    comment            TEXT         NULL,
    status             VARCHAR(16)  NOT NULL DEFAULT 'candidate', -- candidate|approved|rejected|exported
    checks             JSON         NULL,                         -- curated expectations (must_contain, rubric, ...)
    created_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_eval_cases_source (source_request_id),
    KEY idx_eval_cases_topic_status (topic, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Offline (dataset) evaluation runs
CREATE TABLE IF NOT EXISTS eval_runs (
    id                BIGINT        NOT NULL AUTO_INCREMENT,
    kind              VARCHAR(16)   NOT NULL,          -- offline
    topic             VARCHAR(64)   NOT NULL,
    git_sha           VARCHAR(64)   NULL,
    label             VARCHAR(255)  NULL,              -- e.g. PR number, 'nightly'
    cases             INT           NOT NULL,
    pass_rate         DECIMAL(5,4)  NULL,
    mean_judge_score  DECIMAL(4,2)  NULL,
    mean_cost_usd     DECIMAL(12,6) NULL,
    p95_latency_ms    INT           NULL,
    gate_passed       BOOLEAN       NULL,
    summary           JSON          NULL,
    created_at        DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_eval_runs_topic_created (topic, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Per-case results: offline (eval_run_id set, kind='offline') and online judging of
-- sampled production runs (eval_run_id NULL, kind='online').
-- online_request_id enforces "judge each production run at most once" (MySQL has no
-- partial unique index; a stored generated column + UNIQUE gives the same guarantee).
CREATE TABLE IF NOT EXISTS eval_results (
    id                 BIGINT        NOT NULL AUTO_INCREMENT,
    kind               VARCHAR(16)   NOT NULL,          -- offline|online
    eval_run_id        BIGINT        NULL,
    topic              VARCHAR(64)   NOT NULL,
    case_id            VARCHAR(128)  NULL,
    request_id         VARCHAR(128)  NULL,              -- the agent run that was judged
    passed             BOOLEAN       NULL,
    judge_score        DECIMAL(4,2)  NULL,
    scores             JSON          NULL,              -- per-criterion scores
    failures           JSON          NULL,              -- deterministic check failures
    rationale          TEXT          NULL,
    cost_usd           DECIMAL(12,6) NULL,
    latency_ms         INT           NULL,
    created_at         DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    online_request_id  VARCHAR(128)  GENERATED ALWAYS AS (IF(kind = 'online', request_id, NULL)) STORED,
    PRIMARY KEY (id),
    UNIQUE KEY uq_eval_results_online_request (online_request_id),
    KEY idx_eval_results_topic_created (topic, created_at),
    KEY idx_eval_results_request (request_id),
    CONSTRAINT fk_eval_results_run FOREIGN KEY (eval_run_id) REFERENCES eval_runs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS drift_reports (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    topic         VARCHAR(64)  NOT NULL,
    window_start  DATETIME(6)  NOT NULL,
    window_end    DATETIME(6)  NOT NULL,
    metrics       JSON         NOT NULL,             -- {metric: {current, baseline, delta, flagged}}
    flagged       JSON         NOT NULL,             -- ["judge_score", ...]
    created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_drift_reports_topic_created (topic, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
