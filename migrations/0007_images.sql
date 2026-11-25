-- Pictures shown in answers (e.g. "show me the Model Y"): downloaded once, checked (public address, real image,
-- at most 1.5 MB), served only by GET /api/v1/images/<id>. id = sha256 of the source address.
CREATE TABLE IF NOT EXISTS images (
    id            CHAR(64)      NOT NULL,
    content_type  VARCHAR(32)   NOT NULL,
    bytes         MEDIUMBLOB    NOT NULL,
    source_url    TEXT          NOT NULL,
    alt           VARCHAR(200)  NOT NULL DEFAULT '',
    created_at    DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
