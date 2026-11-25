-- Admin → Settings: every save is a new version (who, when, why). The newest version per name is live, merged
-- over config/<name>.yaml; content = only what differs from the file ({} = the file as is). Rollback = a new
-- version with an old one's content, so the history is never rewritten.
CREATE TABLE IF NOT EXISTS settings_versions (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    name        VARCHAR(32)   NOT NULL,
    content     JSON          NOT NULL,
    author      VARCHAR(255)  NOT NULL,
    comment     VARCHAR(300)  NOT NULL DEFAULT '',
    created_at  DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_settings_name_id (name, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
