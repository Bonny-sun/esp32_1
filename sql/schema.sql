-- ===========================================================================
--  Smart Aquaponics Edge Monitor — PostgreSQL schema (Supabase)
--
--  * Shared-DB-safe: every object is prefixed  aqua_  so it can live in a
--    dedicated project's `public` schema OR be dropped into an existing
--    project without colliding with other tables.
--  * Phase 1 fills  temperature + humidity  only. water_temp / ph /
--    soil_moisture are created NOW as NULLable columns => a hardware
--    upgrade needs ZERO schema migration.
--  * Re-runnable: safe to paste into the Supabase SQL editor repeatedly.
-- ===========================================================================

-- ---------------------------------------------------------------------------
--  1. Device registry
-- ---------------------------------------------------------------------------
create table if not exists public.aqua_devices (
    device_id    text        primary key,
    site_id      text        not null default 'default',
    name         text,
    location     text,
    fw_version   text,
    created_at   timestamptz not null default now(),
    last_seen    timestamptz
);

-- ---------------------------------------------------------------------------
--  2. Raw telemetry time-series
-- ---------------------------------------------------------------------------
create table if not exists public.aqua_telemetry (
    id               bigint      generated always as identity primary key,
    device_id        text        not null references public.aqua_devices(device_id),
    ts               timestamptz not null default now(),  -- device time (NTP, UTC); default = insert time until clock syncs
    server_received  timestamptz not null default now(),  -- ingest time (UTC)
    -- Phase 1 metrics
    temperature      real,
    humidity         real,
    -- Phase 2 metrics — NULLable placeholders (zero-migration upgrade path)
    water_temp       real,
    ph               real,
    soil_moisture    real,
    -- diagnostics + anything the firmware sends that has no column yet
    rssi             integer,
    extra            jsonb       not null default '{}'::jsonb
);

create index if not exists aqua_telemetry_device_ts_idx
    on public.aqua_telemetry (device_id, ts desc);
create index if not exists aqua_telemetry_ts_brin_idx
    on public.aqua_telemetry using brin (ts);
create index if not exists aqua_telemetry_extra_gin_idx
    on public.aqua_telemetry using gin (extra);

-- ---------------------------------------------------------------------------
--  3. Hourly rollup — keeps storage bounded under the 500 MB free tier
-- ---------------------------------------------------------------------------
create table if not exists public.aqua_telemetry_hourly (
    device_id          text        not null references public.aqua_devices(device_id),
    bucket             timestamptz not null,               -- date_trunc('hour', ts)
    n_samples          integer     not null,
    temperature_avg    real, temperature_min real, temperature_max real,
    humidity_avg       real, humidity_min real, humidity_max real,
    water_temp_avg     real,
    ph_avg             real,
    soil_moisture_avg  real,
    primary key (device_id, bucket)
);

-- ---------------------------------------------------------------------------
--  4. AI outputs
-- ---------------------------------------------------------------------------
create table if not exists public.aqua_forecasts (
    id          bigint      generated always as identity primary key,
    device_id   text        not null references public.aqua_devices(device_id),
    metric      text        not null,            -- 'temperature', 'humidity', ...
    horizon_min integer     not null,
    ts_target   timestamptz not null,
    yhat        real        not null,
    yhat_lower  real,
    yhat_upper  real,
    model       text        not null,
    created_at  timestamptz not null default now()
);
create index if not exists aqua_forecasts_lookup_idx
    on public.aqua_forecasts (device_id, metric, ts_target desc);

create table if not exists public.aqua_anomalies (
    id          bigint      generated always as identity primary key,
    device_id   text        not null references public.aqua_devices(device_id),
    ts          timestamptz not null,
    metric      text        not null,
    value       real,
    score       real,
    method      text        not null,
    note        text,
    created_at  timestamptz not null default now()
);
create index if not exists aqua_anomalies_lookup_idx
    on public.aqua_anomalies (device_id, ts desc);

-- ---------------------------------------------------------------------------
--  5. Convenience view: newest row per device (dashboard / ML)
-- ---------------------------------------------------------------------------
create or replace view public.aqua_latest as
select distinct on (device_id)
       device_id, ts, temperature, humidity, water_temp, ph, soil_moisture, rssi
from   public.aqua_telemetry
order  by device_id, ts desc;

-- ===========================================================================
--  6. Row Level Security
--     * writes come from the service_role key (worker + AI job) which
--       BYPASSES RLS entirely.
--     * anon key (public dashboard) is read-only.
-- ===========================================================================
alter table public.aqua_devices          enable row level security;
alter table public.aqua_telemetry        enable row level security;
alter table public.aqua_telemetry_hourly enable row level security;
alter table public.aqua_forecasts        enable row level security;
alter table public.aqua_anomalies        enable row level security;

drop policy if exists aqua_devices_read on public.aqua_devices;
drop policy if exists aqua_tel_read     on public.aqua_telemetry;
drop policy if exists aqua_tel_h_read   on public.aqua_telemetry_hourly;
drop policy if exists aqua_fc_read      on public.aqua_forecasts;
drop policy if exists aqua_anom_read    on public.aqua_anomalies;

create policy aqua_devices_read on public.aqua_devices          for select to anon using (true);
create policy aqua_tel_read     on public.aqua_telemetry        for select to anon using (true);
create policy aqua_tel_h_read   on public.aqua_telemetry_hourly for select to anon using (true);
create policy aqua_fc_read      on public.aqua_forecasts        for select to anon using (true);
create policy aqua_anom_read    on public.aqua_anomalies        for select to anon using (true);

-- ===========================================================================
--  7. Retention + rollup jobs   (needs pg_cron:
--     Supabase Dashboard -> Database -> Extensions -> enable "pg_cron")
-- ===========================================================================
create extension if not exists pg_cron;

create or replace function public.aqua_rollup_hourly()
returns void language sql as $fn$
    insert into public.aqua_telemetry_hourly as h
        (device_id, bucket, n_samples,
         temperature_avg, temperature_min, temperature_max,
         humidity_avg, humidity_min, humidity_max,
         water_temp_avg, ph_avg, soil_moisture_avg)
    select device_id,
           date_trunc('hour', ts)          as bucket,
           count(*),
           avg(temperature), min(temperature), max(temperature),
           avg(humidity),    min(humidity),    max(humidity),
           avg(water_temp),  avg(ph),          avg(soil_moisture)
    from   public.aqua_telemetry
    where  ts >= now() - interval '3 hours'
    group  by device_id, date_trunc('hour', ts)
    on conflict (device_id, bucket) do update set
        n_samples         = excluded.n_samples,
        temperature_avg   = excluded.temperature_avg,
        temperature_min   = excluded.temperature_min,
        temperature_max   = excluded.temperature_max,
        humidity_avg      = excluded.humidity_avg,
        humidity_min      = excluded.humidity_min,
        humidity_max      = excluded.humidity_max,
        water_temp_avg    = excluded.water_temp_avg,
        ph_avg            = excluded.ph_avg,
        soil_moisture_avg = excluded.soil_moisture_avg;
$fn$;

create or replace function public.aqua_prune_raw()
returns void language sql as $fn$
    delete from public.aqua_telemetry where ts < now() - interval '30 days';
$fn$;

-- pg_cron: re-running cron.schedule with the same job name updates it.
select cron.schedule('aqua_rollup_hourly', '*/15 * * * *', $$ select public.aqua_rollup_hourly(); $$);
select cron.schedule('aqua_prune_raw',     '10 3 * * *',   $$ select public.aqua_prune_raw();     $$);

-- ---------------------------------------------------------------------------
--  8. Seed the first device (edit to match include/secrets.h)
-- ---------------------------------------------------------------------------
insert into public.aqua_devices (device_id, site_id, name, location, fw_version)
values ('esp32-aqua-01', 'home', 'Water Guardian #1', 'desk prototype', '0.1.0')
on conflict (device_id) do nothing;
