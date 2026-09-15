-- Deferred collection answers. Safe to rerun after 002_agent_catalog.sql.
ALTER TABLE collection_subscribers ADD COLUMN IF NOT EXISTS reply_queued_at TIMESTAMPTZ;

-- The release that introduces replies may meet a task completed by the old
-- worker minutes earlier. Re-run only recent, still-current requests that have
-- no durable answer. Once an answer is queued, reply_queued_at makes this a
-- no-op on every later deploy.
UPDATE collection_tasks t
SET status='pending', attempts=0, run_after=now(), lease_token=NULL,
    lease_until=NULL, deadline_at=NULL, error_code=NULL
WHERE t.status='done'
  AND t.created_at >= now() - interval '24 hours'
  AND EXISTS (
      SELECT 1
      FROM collection_subscribers s
      JOIN passports p ON p.user_id=s.user_id
          AND COALESCE(p.root_id,p.id)=s.request_id
          AND p.version=s.request_version AND p.is_current
      WHERE s.task_id=t.id AND s.active AND s.reply_queued_at IS NULL
  );
