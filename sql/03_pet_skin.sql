-- ===========================================================================
--  03_pet_skin.sql — remember which OLED pet skin each device is showing.
--  Incremental migration. Run in the Supabase SQL editor AFTER schema.sql
--  and 02_thresholds.sql. Re-runnable.
--
--  pet_skin is device-level state (like fw_version), not a time-series
--  metric, so it lives on aqua_devices rather than aqua_telemetry. The
--  worker updates it from the telemetry payload's top-level "pet" field
--  (whatever the firmware last actually applied); the dashboard both
--  reads it (to show current state) and writes the *desired* value by
--  publishing an MQTT command — this column is not itself the source of
--  truth for the ESP32, just a mirror of it for the UI.
-- ===========================================================================

alter table public.aqua_devices
    add column if not exists pet_skin text not null default 'drop';
