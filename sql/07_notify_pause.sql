-- ===========================================================================
--  07_notify_pause.sql — a global "pause LINE push" switch, toggled from the
--  dashboard. Incremental migration. Run in the Supabase SQL editor.
--  Re-runnable.
--
--  Generic key/value settings table so future one-off toggles don't each
--  need their own migration + column. worker/ingest.py checks this before
--  every LINE push; the dashboard flips it via a switch in the header.
-- ===========================================================================

create table if not exists public.aqua_settings (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

insert into public.aqua_settings (key, value)
values ('line_push_paused', 'false'::jsonb)
on conflict (key) do nothing;

alter table public.aqua_settings enable row level security;

drop policy if exists aqua_settings_read on public.aqua_settings;
create policy aqua_settings_read on public.aqua_settings for select to anon using (true);
