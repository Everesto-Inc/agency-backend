-- Config tests (Admin → Experiments): run a dataset against the LIVE config and a DRAFT, side by side.
-- Queued by the edge, run by the quality worker (CronJob agency-experiments). results = one entry per case with
-- both variants (answer excerpt, checks, judge score, cost, time, models).
CREATE TABLE IF NOT EXISTS config_tests (
    id           BIGINT        NOT NULL AUTO_INCREMENT,
    dataset_id   BIGINT        NOT NULL,
    draft_id     BIGINT        NULL,
    max_cases    INT           NOT NULL,
    status       VARCHAR(16)   NOT NULL DEFAULT 'queued',
    created_by   VARCHAR(255)  NOT NULL,
    created_at   DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    started_at   DATETIME(6)   NULL,
    finished_at  DATETIME(6)   NULL,
    done_cases   INT           NOT NULL DEFAULT 0,
    total_cases  INT           NOT NULL DEFAULT 0,
    summary      JSON          NULL,
    results      JSON          NULL,
    error        VARCHAR(500)  NULL,
    PRIMARY KEY (id),
    KEY idx_config_tests_status (status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
