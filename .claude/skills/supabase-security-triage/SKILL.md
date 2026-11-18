---
name: supabase-security-triage
description: Use when the user reports a Supabase Security Advisor finding (RLS Disabled in Public, Security Definer View, or similar) for this project's database, or asks to review/fix RLS policies. Triages which findings are this repo's responsibility before writing any SQL.
---

# Supabase Security Advisor triage

## Why this project needs its own triage step

The Supabase project backing this app is literally named `auto-stock-picker`
— it's the user's pre-existing, unrelated stock-picker app's project. It was
reused here (Supabase free tier caps a user at 2 projects) with every table
this repo owns prefixed `aqua_` as the *only* isolation between the two apps
(see `sql/schema.sql`'s own "Shared-DB-safe" header comment). The two apps'
tables sit in the same `public` schema, so a Security Advisor scan covers
both at once — most findings it reports may belong to the *other* app, not
this one.

Never guess a schema or write an RLS policy for a table that isn't this
repo's. Guessing wrong here isn't a no-op: an `auth.uid() = user_id`-style
policy assumes Supabase Auth, but the other app might use its own
username/password system entirely outside Supabase Auth (confirmed once
already — its `watchlist` table has a plain `text` `username` column, not a
Supabase-Auth `uuid`). A wrong policy can silently pass everything through,
or lock out real users of an app this session has no visibility into to
even notice the breakage.

## Triage steps

1. **Grep this repo for every table/view name the advisor lists.**
   ```
   grep -rli "<table_name>" --include="*.sql" --include="*.py" .
   ```
   This repo's own tables/views all start with `aqua_` (see
   `sql/schema.sql`). A name with no `aqua_` prefix and no hits in this
   repo belongs to the other app — stop here for that finding, tell the
   user, and don't write SQL for it. Point them at the other app's own
   repo/session if they want it fixed from code, or let them fix it
   directly in the Supabase SQL editor themselves.

2. **For a finding that IS this repo's** (an `aqua_*` table/view, or
   confirmed present in this codebase):
   - **RLS Disabled in Public**: read how this repo already does RLS
     (`sql/schema.sql` section 6, or any `sql/0N_*.sql` file) before writing
     a new policy — the established pattern here is: `service_role` (the
     worker + dashboard's own writes) bypasses RLS entirely by design, and
     `anon` gets `for select to anon using (true)` on tables meant to be
     publicly readable (telemetry, forecasts, anomalies, thresholds, the
     FAQ/`aqua_kb`), or **no policy at all** on tables that should stay
     fully private even from `anon` (e.g. `aqua_auth_log` — see
     `sql/10_auth_log.sql`'s own comment on why that's deliberate, not an
     oversight). Match whichever of those two shapes the table actually
     needs — don't default to "anon read-only" without checking whether
     the table holds anything sensitive.
   - **Security Definer View**: a view runs with its *owner's* privileges
     by default (bypassing the underlying tables' RLS) unless
     `security_invoker = true` is set. Check whether the view's underlying
     table(s) already grant the access the view exposes anyway — if so
     (as with `aqua_latest` over `aqua_telemetry`, both anon-readable),
     flipping on `security_invoker` is a pure cleanup with zero behavior
     change. If the view genuinely needs elevated access the querying role
     doesn't have, say so explicitly and confirm the underlying tables are
     still protected by their own RLS rather than relying on the view as
     the only gate.
   - **Function Search Path Mutable**: a function with no `SET
     search_path` resolves any unqualified object name using whatever
     search_path the *caller's session* happens to have, not a fixed one
     — exploitable by anyone who can create an object in a schema earlier
     in that path. Read the function's actual body before picking a fix:
     if every object reference inside it is already schema-qualified
     (`public.aqua_*`, as all of this project's functions normally are —
     check, don't assume), lock it to an **empty** search_path
     (`alter function public.fn(args) set search_path = '';`) — the
     strictest option. If the body uses an extension's operator/type
     unqualified (pgvector's `<=>`, `vector`), an empty path will break
     it — pin to whichever schema(s) it actually needs instead (e.g.
     `public` or `extensions`, see below), which still closes the finding
     because the path is now fixed and caller-independent, just not
     empty.
   - **Extension in Public**: an extension installed with no `SCHEMA`
     clause lands in `public` by default, mixing its objects into the
     same schema `anon`/`authenticated` already read from. Supabase
     projects ship a dedicated `extensions` schema for this — move it
     there: `create schema if not exists extensions; alter extension
     <name> set schema extensions;`. This only relocates the extension's
     own objects; a table column using one of its types keeps working
     unchanged (Postgres tracks a column's type by OID, not by
     schema-qualified name). Any function that uses that extension's
     operators/types unqualified needs `extensions` added to its own
     `search_path` afterward (see the Function Search Path fix above) —
     do both in the same migration, not separately, so there's no window
     where the function is broken.

3. **Write the fix as a new numbered file** following the existing
   `sql/0N_description.sql` sequence (re-runnable, header comment
   explaining what it does and why, same style as `sql/10_auth_log.sql` or
   `sql/12_aqua_latest_security_invoker.sql`). Don't hand-edit
   `sql/schema.sql` for an incremental fix — it's the from-scratch bootstrap
   script, the numbered files are the migration history.

4. **Record the triage outcome** (what was this repo's, what wasn't, and
   why) in `docs/architecture.md`'s decisions log — especially the "not
   this repo's, didn't touch it" findings, so a future advisor re-scan
   doesn't re-trigger the same guess-the-schema question.

5. **Testing a new RLS policy**: verify with the `anon` key specifically
   (not `service_role`, which bypasses RLS entirely and will look fine
   either way) — e.g. via `curl` against PostgREST with the anon API key,
   or the Supabase SQL editor's "Run as" role switcher if available. Confirm
   a row the policy should hide is actually absent from the result, not
   just that the query doesn't error.
