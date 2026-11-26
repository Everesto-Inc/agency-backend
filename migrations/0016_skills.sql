-- Skills (Admin → Skills): instruction packs in the open Agent Skills format (a folder with SKILL.md), installed
-- from GitHub pinned to one commit. Text only — scripts are never stored or run. Files live here (small text),
-- so every assistant can read them without bucket keys.
--   skill_versions  one row per install / update (immutable)
--   skill_files     the text files of a version (SKILL.md, references/*.md …)
--   skills          per skill: which version is active, on/off, which assistants may use it
CREATE TABLE IF NOT EXISTS skill_versions (
    id           BIGINT        NOT NULL AUTO_INCREMENT,
    name         VARCHAR(64)   NOT NULL,
    version      INT           NOT NULL,
    description  VARCHAR(1024) NOT NULL,
    source_url   VARCHAR(500)  NOT NULL,
    repo         VARCHAR(200)  NOT NULL,
    ref          VARCHAR(100)  NOT NULL,
    commit_sha   CHAR(40)      NOT NULL,
    path         VARCHAR(300)  NOT NULL,
    licence      VARCHAR(64)   NOT NULL,
    files        INT           NOT NULL,
    total_bytes  INT           NOT NULL,
    skipped      JSON          NOT NULL,
    warnings     JSON          NOT NULL,
    installed_by VARCHAR(255)  NOT NULL,
    comment      VARCHAR(300)  NOT NULL DEFAULT '',
    created_at   DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_skill_versions_name_version (name, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS skill_files (
    version_id  BIGINT        NOT NULL,
    path        VARCHAR(300)  NOT NULL,
    size_bytes  INT           NOT NULL,
    content     MEDIUMTEXT    NOT NULL,
    PRIMARY KEY (version_id, path),
    CONSTRAINT fk_skill_files_version FOREIGN KEY (version_id) REFERENCES skill_versions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS skills (
    name              VARCHAR(64)  NOT NULL,
    active_version_id BIGINT       NOT NULL,
    enabled           BOOLEAN      NOT NULL DEFAULT FALSE,
    assistants        JSON         NOT NULL,
    updated_by        VARCHAR(255) NOT NULL,
    updated_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (name),
    CONSTRAINT fk_skills_active_version FOREIGN KEY (active_version_id) REFERENCES skill_versions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Which skills a run used (Admin → Runs), e.g. "stock-analysis@v2".
ALTER TABLE agent_runs ADD COLUMN skills VARCHAR(300) NULL AFTER error_message;
