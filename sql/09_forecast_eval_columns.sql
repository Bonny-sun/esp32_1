-- ===========================================================================
--  09_forecast_eval_columns.sql — pre-computed 「預測 vs 實際」比對結果。
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  actual/err/hit start NULL and stay that way until baseline.py's
--  settle_due_forecasts() finds a real telemetry reading near ts_target
--  (same nearest-within-15-min match the dashboard used to do on every
--  /forecast page load) and fills them in, once, in the background.
--  Moves that match-and-compare work off the request path entirely —
--  see docs/architecture.md decisions log for the reasoning.
-- ===========================================================================

alter table public.aqua_forecasts
    add column if not exists actual real,
    add column if not exists err    real,
    add column if not exists hit    boolean;

-- Partial index: only the still-unsettled rows need to be found quickly
-- (settle_due_forecasts() scans for these every run); settled rows are
-- read by device_id + ts_target, already covered by the existing lookup
-- index.
create index if not exists aqua_forecasts_unsettled_idx
    on public.aqua_forecasts (device_id, ts_target)
    where actual is null;
