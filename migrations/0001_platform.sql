-- Platform schema: identity, subscriptions, agent runs, LLM call ledger, usage rollups.
--
-- Target: MySQL 8.4 locally, OCI HeatWave MySQL in prod. Conventions:
--   * every table has a PRIMARY KEY (HeatWave enforces sql_require_primary_key)
--   * timestamps are DATETIME(6) in UTC (sessions run with time_zone = '+00:00')
--   * UUIDs are CHAR(36); money is DECIMAL; booleans are BOOLEAN (TINYINT(1))
--   * statements are re-runnable (MySQL DDL is not transactional; see db/migrate.py)

CREATE TABLE IF NOT EXISTS users (
    id               CHAR(36)     NOT NULL,
    email            VARCHAR(320) NOT NULL,
    hashed_password  VARCHAR(255) NOT NULL,
    role             VARCHAR(32)  NOT NULL DEFAULT 'user',        -- user|admin|service
    plan             VARCHAR(64)  NOT NULL DEFAULT 'free',
    plan_expires_at  DATETIME(6)  NULL,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at       DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    user_id     CHAR(36)    NOT NULL,
    token_hash  CHAR(64)    NOT NULL,
    family_id   CHAR(36)    NOT NULL,
    expires_at  DATETIME(6) NOT NULL,
    revoked_at  DATETIME(6) NULL,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_refresh_tokens_hash (token_hash),
    KEY idx_refresh_tokens_user (user_id),
    KEY idx_refresh_tokens_family (family_id),
    CONSTRAINT fk_refresh_tokens_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- One row per agent run (any topic, any framework).
CREATE TABLE IF NOT EXISTS agent_runs (
    request_id          VARCHAR(128)  NOT NULL,
    user_id             CHAR(36)      NOT NULL,
    topic               VARCHAR(64)   NOT NULL,
    session_id          VARCHAR(64)   NOT NULL,
    prompt              MEDIUMTEXT    NULL,             -- PII-redacted; nulled by retention
    answer              MEDIUMTEXT    NULL,             -- PII-redacted; nulled by retention
    status              VARCHAR(16)   NOT NULL,         -- running|success|error|timeout|cancelled
    error_code          VARCHAR(64)   NULL,
    latency_ms          INT           NULL,
    prompt_tokens       INT           NOT NULL DEFAULT 0,
    completion_tokens   INT           NOT NULL DEFAULT 0,
    cost_usd            DECIMAL(12,6) NOT NULL DEFAULT 0,
    llm_calls           INT           NOT NULL DEFAULT 0,
    answer_chars        INT           NULL,
    cache_hit           BOOLEAN       NOT NULL DEFAULT FALSE,
    eval_sampled        BOOLEAN       NOT NULL DEFAULT FALSE,   -- picked for online LLM-judge scoring
    prompt_fingerprint  VARCHAR(255)  NULL,                     -- prompt versions + A/B arms used
    feedback_score      SMALLINT      NULL,
    feedback_comment    TEXT          NULL,
    created_at          DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at         DATETIME(6)   NULL,
    PRIMARY KEY (request_id),
    KEY idx_agent_runs_user_created (user_id, created_at),
    KEY idx_agent_runs_topic_created (topic, created_at),
    KEY idx_agent_runs_sampled (eval_sampled, created_at),
    CONSTRAINT fk_agent_runs_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- One row per LLM attempt through the model gateway (failed attempts and
-- fallbacks included) -- the basis for per-model performance tracking.
-- user_id has no FK on purpose: erasure anonymises rows (NULL) so aggregate
-- cost accounting stays correct.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 BIGINT        NOT NULL AUTO_INCREMENT,
    request_id         VARCHAR(128)  NOT NULL,
    user_id            CHAR(36)      NULL,
    client_id          VARCHAR(64)   NOT NULL,          -- calling service (= topic, or edge/quality)
    task               VARCHAR(64)   NULL,
    prompt_ref         VARCHAR(255)  NULL,              -- e.g. ev_research/synthesize@2.0~b
    requested_model    VARCHAR(128)  NOT NULL,          -- alias as requested: auto / task-x / tier-y
    route_source       VARCHAR(32)   NOT NULL,          -- task|tier|classifier|judge|default
    route_reason       TEXT          NULL,
    tier               VARCHAR(16)   NOT NULL,
    model              VARCHAR(128)  NOT NULL,          -- catalog model id actually used
    attempt            SMALLINT      NOT NULL DEFAULT 1,
    status             VARCHAR(16)   NOT NULL,          -- success|error
    error_type         VARCHAR(128)  NULL,
    prompt_tokens      INT           NOT NULL DEFAULT 0,
    completion_tokens  INT           NOT NULL DEFAULT 0,
    cost_usd           DECIMAL(12,6) NOT NULL DEFAULT 0,
    latency_ms         INT           NOT NULL,
    streamed           BOOLEAN       NOT NULL DEFAULT FALSE,
    cached             BOOLEAN       NOT NULL DEFAULT FALSE,
    canary             BOOLEAN       NOT NULL DEFAULT FALSE,
    pii_redactions     INT           NOT NULL DEFAULT 0,
    created_at         DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_llm_calls_created (created_at),
    KEY idx_llm_calls_model_created (model, created_at),
    KEY idx_llm_calls_client_created (client_id, created_at),
    KEY idx_llm_calls_request (request_id),
    KEY idx_llm_calls_user_created (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Per-user x per-topic x day usage: the billing / "my usage" source of truth.
-- Runs are added by topic runners, tokens/cost by the model gateway.
-- Pseudo-topics: 'routing' (edge /chat classification), 'evals' (quality jobs).
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id            CHAR(36)      NOT NULL,
    topic              VARCHAR(64)   NOT NULL,
    day                DATE          NOT NULL,
    runs               INT           NOT NULL DEFAULT 0,
    successful_runs    INT           NOT NULL DEFAULT 0,
    cached_runs        INT           NOT NULL DEFAULT 0,
    llm_calls          INT           NOT NULL DEFAULT 0,
    prompt_tokens      BIGINT        NOT NULL DEFAULT 0,
    completion_tokens  BIGINT        NOT NULL DEFAULT 0,
    cost_usd           DECIMAL(14,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, topic, day),
    KEY idx_usage_daily_day (day),
    KEY idx_usage_daily_topic_day (topic, day),
    CONSTRAINT fk_usage_daily_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
