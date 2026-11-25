-- Test datasets (Admin → Experiments): JSONL files in the bucket (agency/datasets/<name>/v<version>.jsonl),
-- one row per version. Uploading a file with an existing name adds a version; old versions stay (results of
-- old test runs point at them).
CREATE TABLE IF NOT EXISTS eval_datasets (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)   NOT NULL,
    version     INT           NOT NULL,
    cases       INT           NOT NULL,
    topics      JSON          NOT NULL,
    size_bytes  INT           NOT NULL,
    object_key  VARCHAR(300)  NOT NULL,
    author      VARCHAR(255)  NOT NULL,
    comment     VARCHAR(300)  NOT NULL DEFAULT '',
    created_at  DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_eval_datasets_name_version (name, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
