-- Pictures in chats. The picture itself is in OCI Object Storage (bucket dev-agency-media, key = object_key);
-- this table only says what it is and who may see it.
--   kind web:    found on the web for an answer (e.g. "show me the Model Y") — shared, id = sha256 of the source
--   kind upload: sent by a user with a question — private to that user, deleted with the chat / account /
--                after the retention period. request_id = the question it was sent with.
-- description: one line written by a vision model; the assistants and the chat memory use it, never the picture.
CREATE TABLE IF NOT EXISTS images (
    id            CHAR(64)      NOT NULL,
    kind          VARCHAR(8)    NOT NULL,
    object_key    VARCHAR(300)  NOT NULL,
    content_type  VARCHAR(32)   NOT NULL,
    size_bytes    INT           NOT NULL,
    user_id       CHAR(36)      NULL,
    session_id    VARCHAR(128)  NULL,
    request_id    VARCHAR(128)  NULL,
    source_url    TEXT          NULL,
    alt           VARCHAR(200)  NOT NULL DEFAULT '',
    description   VARCHAR(1000) NULL,
    created_at    DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_images_user_session (user_id, session_id),
    KEY idx_images_request (request_id),
    KEY idx_images_kind_created (kind, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
