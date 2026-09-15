-- Wake recent unanswered collection tasks after the guaranteed-reply release.
-- Safe on every deploy: reply_queued_at permanently excludes answered requests.
UPDATE collection_tasks t
SET status='pending', attempts=LEAST(t.attempts,t.max_attempts-1),
    run_after=now(), lease_token=NULL, lease_until=NULL, deadline_at=NULL
WHERE t.status IN ('pending','failed','done')
  AND t.created_at >= now() - interval '24 hours'
  AND EXISTS (
      SELECT 1
      FROM collection_subscribers s
      JOIN passports p ON p.user_id=s.user_id
          AND COALESCE(p.root_id,p.id)=s.request_id
          AND p.version=s.request_version AND p.is_current
      WHERE s.task_id=t.id AND s.active AND s.reply_queued_at IS NULL
  );
