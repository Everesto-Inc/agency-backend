-- Every time a guardrail fires (quota, rate limit, injection attempt, PII masked, number not on the source page,
-- unsafe address, refused upload…). Never the user's text: only what fired, where, and short technical detail.
-- Admin → Guardrails reads it; retention: privacy.yaml guardrail_events_days.
CREATE TABLE IF NOT EXISTS guardrail_events (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    created_at  DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    service     VARCHAR(40)   NOT NULL,
    kind        VARCHAR(32)   NOT NULL,
    action      VARCHAR(16)   NOT NULL,
    topic       VARCHAR(64)   NULL,
    user_id     CHAR(36)      NULL,
    request_id  VARCHAR(128)  NULL,
    detail      VARCHAR(500)  NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    KEY idx_guardrail_created (created_at),
    KEY idx_guardrail_kind_created (kind, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
