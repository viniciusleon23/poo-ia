-- Documentation citations are never an execution destination. Keep the historic
-- jobs intact while removing the polluted context from existing conversations.
UPDATE conversations
SET active_repository = NULL, last_job_id = NULL
WHERE lower(active_repository) IN ('brain-capnet', 'capnet-brain');
