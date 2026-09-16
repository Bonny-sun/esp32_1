-- ===========================================================================
--  06_anomaly_bounds.sql — store the breached band on the anomaly row.
--  Incremental migration. Run BEFORE re-running 02_thresholds.sql (which
--  this turn also updates to populate the new columns). Re-runnable.
--
--  aqua_check_thresholds() previously only encoded the band into the
--  English `note` text ("temperature 30.9 out of band [22, 30.5]"), so any
--  consumer that wanted min/max structured (e.g. the LINE push message)
--  had to parse that string. Real columns instead.
-- ===========================================================================

alter table public.aqua_anomalies
    add column if not exists min_val real,
    add column if not exists max_val real;
