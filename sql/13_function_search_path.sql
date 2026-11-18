-- ===========================================================================
--  13_function_search_path.sql — fixes Supabase Security Advisor's
--  "Function Search Path Mutable" findings (5x) and "Extension in Public"
--  finding, all on this repo's own aqua_* objects (confirmed by grep
--  before writing this — see docs/architecture.md's decision log entry).
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  Function Search Path Mutable: a function with no SET search_path
--  clause resolves any unqualified object name using whatever
--  search_path the CALLING session happens to have — which anyone with
--  CREATE privilege on some schema earlier in that path could exploit to
--  substitute a malicious object for the one the function meant to call.
--  The fix is to pin each function's own search_path explicitly, so it
--  never depends on the caller's.
--
--  aqua_rollup_hourly / aqua_prune_raw / aqua_check_thresholds /
--  aqua_prune_auth_log already fully-qualify every table reference in
--  their bodies (public.aqua_*) — checked each one's actual body before
--  writing this, not assumed — so they're locked to an EMPTY
--  search_path, the strictest setting: nothing unqualified can resolve
--  at all, and nothing in their bodies needs to.
--
--  match_aqua_kb is different: it uses pgvector's `<=>` operator and
--  `vector` type unqualified in its body. Its search_path is set below,
--  AFTER moving pgvector out of `public` (see the second part of this
--  file), to wherever pgvector ends up — still a FIXED, explicit path
--  (which is what closes this advisory item), just not the empty one,
--  because this function genuinely needs to resolve pgvector's operator.
-- ===========================================================================

alter function public.aqua_rollup_hourly()    set search_path = '';
alter function public.aqua_prune_raw()        set search_path = '';
alter function public.aqua_check_thresholds() set search_path = '';
alter function public.aqua_prune_auth_log()   set search_path = '';

-- ===========================================================================
--  Extension in Public: pgvector was installed into the `public` schema
--  (sql/11_knowledge_base.sql's `create extension if not exists vector;`,
--  with no SCHEMA clause — Postgres/Supabase's default when none is
--  given). Supabase's own guidance is to keep extensions out of `public`
--  and in a dedicated schema instead, so extension-provided objects
--  don't sit in the same schema anon/authenticated already have broad
--  read access to.
--
--  Supabase projects come with an `extensions` schema already set up for
--  exactly this; `create schema if not exists` below is just a
--  defensive no-op if that's ever not already true.
--
--  Moving the extension only relocates pgvector's OWN objects (the
--  `vector` type, the `<=>`/`<->`/`<#>` operators). It does NOT touch
--  aqua_kb.embedding's actual column data — Postgres tracks a column's
--  type by OID internally, not by schema-qualified name, so the existing
--  table/column/rows keep working unchanged — and it does NOT move
--  match_aqua_kb() itself, which stays in `public` (PostgREST only
--  exposes RPCs from `public`, so it needs to stay there regardless).
--  match_aqua_kb()'s search_path is updated below to include
--  `extensions`, since that's where pgvector's `<=>` operator now lives.
-- ===========================================================================

create schema if not exists extensions;
alter extension vector set schema extensions;

alter function public.match_aqua_kb(vector, int) set search_path = extensions, public, pg_catalog;
