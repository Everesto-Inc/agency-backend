-- Config drafts (Admin → Settings → Import / Save as draft; Admin → Experiments tests them):
-- content = {section: override over the file}, same shape as settings_versions. Applying a draft writes one
-- settings_versions row per changed section; the draft itself is kept (status applied) for the record.
CREATE TABLE IF NOT EXISTS settings_drafts (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    content     JSON          NOT NULL,
    based_on    JSON          NOT NULL,
    author      VARCHAR(255)  NOT NULL,
    comment     VARCHAR(300)  NOT NULL DEFAULT '',
    source      VARCHAR(16)   NOT NULL DEFAULT 'edit',
    status      VARCHAR(16)   NOT NULL DEFAULT 'draft',
    created_at  DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    decided_at  DATETIME(6)   NULL,
    decided_by  VARCHAR(255)  NULL,
    PRIMARY KEY (id),
    KEY idx_settings_drafts_status (status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
