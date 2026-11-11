-- ===========================================================================
--  10_auth_log.sql — /admin、/ask 登入事件紀錄（成功／失敗／被鎖定）。
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  No RLS select policy for `anon` is created on purpose — unlike the
--  telemetry/forecast/anomaly tables, this one is read ONLY by the
--  dashboard's service-role client (sb(), not sb_read()). A failed-login
--  audit log is more useful to an attacker than to a public reader, so
--  the default (RLS on, no policy = nobody without service_role can read
--  it at all) is the correct posture here, not an oversight.
--
--  30-day retention via pg_cron, same pattern as aqua_prune_raw() in
--  schema.sql. The dashboard only ever displays the latest 100 rows
--  (a page-size limit, independent of how long rows are kept).
-- ===========================================================================

create table if not exists public.aqua_auth_log (
    id         bigint      generated always as identity primary key,
    ts         timestamptz not null default now(),
    page       text        not null,   -- 'admin' or 'ask' — which login form
    username   text        not null,   -- the account name typed in, as typed
    event      text        not null check (event in ('success', 'failed', 'blocked'))
);

create index if not exists aqua_auth_log_ts_idx
    on public.aqua_auth_log (ts desc);

alter table public.aqua_auth_log enable row level security;
-- (no policies created — service_role bypasses RLS for the dashboard's own
-- reads/writes; anon has no policy, so anon gets zero rows, by design)

create or replace function public.aqua_prune_auth_log()
returns void language sql as $fn$
    delete from public.aqua_auth_log where ts < now() - interval '30 days';
$fn$;

select cron.schedule('aqua_prune_auth_log', '20 3 * * *', $$ select public.aqua_prune_auth_log(); $$);
