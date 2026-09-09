-- ===========================================================================
--  04_pet_temp.sql — per-device OLED pet-expression thresholds.
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  pet_hot  : temp (°C) above which the pet looks HOT (sweaty)
--  pet_cold : temp (°C) below which the pet looks COLD (shivering)
--  These are device state (like pet_skin), set from the dashboard which
--  publishes a retained MQTT {"pet_hot":X,"pet_cold":Y} to .../cmd; the
--  worker mirrors the value the firmware echoes back in telemetry.
-- ===========================================================================

alter table public.aqua_devices
    add column if not exists pet_hot  real not null default 28,
    add column if not exists pet_cold real not null default 18;
