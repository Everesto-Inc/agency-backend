-- When each LLM call STARTED (created_at = when the batched row was written, seconds later) — Admin → Runs draws
-- every call of a question on a timeline.
ALTER TABLE llm_calls ADD COLUMN started_at DATETIME(6) NULL AFTER created_at;
