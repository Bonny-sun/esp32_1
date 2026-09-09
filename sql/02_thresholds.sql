-- ===========================================================================
--  02_thresholds.sql  — alert thresholds + cloud-side threshold checking
--  Incremental migration. Run in the Supabase SQL editor AFTER schema.sql
--  (which is already applied). Re-runnable.
-- ===========================================================================

create table if not exists public.aqua_thresholds (
    device_id  text        not null references public.aqua_devices(device_id),
    metric     text        not null,     -- temperature|humidity|water_temp|ph|soil_moisture
    min_val    real,
    max_val    real,
    enabled    boolean     not null default true,
    updated_at timestamptz not null default now(),
    primary key (device_id, metric)
);

alter table public.aqua_thresholds enable row level security;
drop policy if exists aqua_thr_read on public.aqua_thresholds;
create policy aqua_thr_read on public.aqua_thresholds for select to anon using (true);

-- default bands for the seed device (Phase-2 metrics start disabled)
insert into public.aqua_thresholds (device_id, metric, min_val, max_val, enabled) values
    ('esp32-aqua-01', 'temperature',    18,  28,  true),
    ('esp32-aqua-01', 'humidity',       40,  85,  true),
    ('esp32-aqua-01', 'water_temp',     20,  30,  false),
    ('esp32-aqua-01', 'ph',            6.0, 7.5,  false),
    ('esp32-aqua-01', 'soil_moisture',  20,  80,  false)
on conflict (device_id, metric) do nothing;

-- ---------------------------------------------------------------------------
--  Cloud-side alerting: compare the latest reading per device+metric against
--  its band; log to aqua_anomalies (method='threshold'), deduped to at most
--  one per device+metric per HOUR (an ongoing breach shouldn't spam the log).
-- ---------------------------------------------------------------------------
create or replace function public.aqua_check_thresholds()
returns void language plpgsql as $fn$
declare r record;
begin
  for r in
    select th.device_id, th.metric, th.min_val, th.max_val, l.ts, l.val
    from public.aqua_thresholds th
    join lateral (
      select ts,
             case th.metric
               when 'temperature'   then temperature
               when 'humidity'      then humidity
               when 'water_temp'    then water_temp
               when 'ph'            then ph
               when 'soil_moisture' then soil_moisture
             end as val
      from public.aqua_telemetry
      where device_id = th.device_id
      order by ts desc
      limit 1
    ) l on true
    where th.enabled
      and l.val is not null
      and (l.val < th.min_val or l.val > th.max_val)
  loop
    if not exists (
      select 1 from public.aqua_anomalies
      where device_id = r.device_id and metric = r.metric
        and method = 'threshold'
        and created_at > now() - interval '1 hour'
    ) then
      insert into public.aqua_anomalies (device_id, ts, metric, value, score, method, note)
      values (r.device_id, r.ts, r.metric, r.val, null, 'threshold',
              format('%s %s out of band [%s, %s]', r.metric, r.val, r.min_val, r.max_val));
    end if;
  end loop;
end $fn$;

-- run every minute (re-running cron.schedule with the same name updates it)
select cron.schedule('aqua_check_thresholds', '* * * * *', $$ select public.aqua_check_thresholds(); $$);
