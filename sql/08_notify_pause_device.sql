-- ===========================================================================
--  08_notify_pause_device.sql — per-device override for LINE push pausing.
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  aqua_devices.line_push_paused: NULL means "no override, follow the
--  global aqua_settings.line_push_paused default"; true/false forces this
--  device's push on/off regardless of the global switch. Same pattern as
--  pet_skin/pet_hot/pet_cold — device-level state living on aqua_devices,
--  not a separate key/value row.
-- ===========================================================================

alter table public.aqua_devices
    add column if not exists line_push_paused boolean;
