-- Why a question failed, in words (Admin → Runs): error_code alone ("AGENT_ERROR") doesn't say what happened.
ALTER TABLE agent_runs ADD COLUMN error_message VARCHAR(500) NULL AFTER error_code;
