ALTER TABLE jobs ADD COLUMN notification_completed_at REAL;

-- A version-one callback that already created its deterministic final outbox
-- completed the durable part of notification. Backfill those jobs so upgrading
-- does not replay unrelated callback side effects; jobs without an outbox stay
-- NULL and are recovered by the scheduler.
UPDATE jobs
SET notification_completed_at = (
    SELECT MIN(outbox.created_at)
    FROM outbox
    WHERE outbox.request_id = jobs.request_id
      AND outbox.kind = 'response'
      AND outbox.dedupe_key = 'final'
)
WHERE EXISTS (
    SELECT 1
    FROM outbox
    WHERE outbox.request_id = jobs.request_id
      AND outbox.kind = 'response'
      AND outbox.dedupe_key = 'final'
);

CREATE INDEX jobs_pending_notification_idx
    ON jobs(notification_completed_at, state, created_at, id);
