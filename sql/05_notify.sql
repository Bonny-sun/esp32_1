-- ===========================================================================
--  05_notify.sql — tracks which threshold breaches have been pushed to LINE.
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  The worker (worker/ingest.py) polls for aqua_anomalies rows where
--  method='threshold' and notified_at is null, pushes a LINE message, then
--  stamps notified_at. aqua_check_thresholds() itself is unchanged — it
--  still writes at most one row per device/metric/hour, which doubles as
--  the LINE notification's rate limit.
-- ===========================================================================

alter table public.aqua_anomalies
    add column if not exists notified_at timestamptz;

-- One-time backfill: silence the pre-existing backlog so turning the
-- feature on doesn't broadcast every old breach at once. Safe to re-run —
-- only touches rows still NULL, so it's a no-op once the backlog is clear.
update public.aqua_anomalies
set notified_at = now()
where method = 'threshold' and notified_at is null;
