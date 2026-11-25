-- Chat history: one row per conversation. The id IS the session_id of the chat (agent memory + agent_runs.session_id),
-- scoped per user. Messages are the agent_runs of that session (prompt/answer, PII-redacted, text nulled by retention).

CREATE TABLE IF NOT EXISTS conversations (
    id          VARCHAR(64)  NOT NULL,                  -- = session_id sent to /chat
    user_id     CHAR(36)     NOT NULL,
    title       VARCHAR(200) NOT NULL,                  -- first question (PII-redacted, shortened), user can rename
    pinned      BOOLEAN      NOT NULL DEFAULT FALSE,
    turns       INT          NOT NULL DEFAULT 0,
    last_topic  VARCHAR(64)  NULL,
    created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),  -- last question (set by the app, not ON UPDATE)
    PRIMARY KEY (user_id, id),
    KEY idx_conversations_user_updated (user_id, pinned, updated_at),
    CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- open one conversation = all runs of (user, session) in order
ALTER TABLE agent_runs ADD INDEX idx_agent_runs_user_session (user_id, session_id, created_at);

-- chats that happened before this table existed (re-runnable: existing rows are left alone)
INSERT IGNORE INTO conversations (id, user_id, title, turns, last_topic, created_at, updated_at)
SELECT session_id, user_id, 'Earlier conversation', COUNT(*), MAX(topic), MIN(created_at), MAX(created_at)
FROM agent_runs
GROUP BY user_id, session_id;
