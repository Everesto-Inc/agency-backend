-- Chat memory, layer 2: a short running summary of the older part of a chat (written by the cheap model in the
-- background, about every 3 questions). summary_upto = time of the newest message already folded into it.
ALTER TABLE conversations ADD COLUMN summary TEXT NULL;

ALTER TABLE conversations ADD COLUMN summary_upto DATETIME(6) NULL;
