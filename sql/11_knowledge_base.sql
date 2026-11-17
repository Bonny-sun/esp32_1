-- ===========================================================================
--  11_knowledge_base.sql — /ask 的 FAQ／操作手冊知識庫（RAG 檢索用）。
--  Incremental migration. Run in the Supabase SQL editor. Re-runnable.
--
--  Unlike aqua_auth_log, this table's content is public FAQ text (device
--  wiring, how to reset Wi-Fi, what the lockout numbers are, ...) — nothing
--  sensitive, so `anon` gets a SELECT policy like every other public
--  dashboard table, and reads go through sb_read() same as the rest.
--  Writes (the one-off embedding build) use service_role, same as every
--  other write path in this project.
--
--  Requires the pgvector extension. match_aqua_kb() is the cosine-distance
--  nearest-neighbour lookup dashboard/app.py's search_kb() calls via
--  sb_read().rpc(...) — a Postgres function rather than a plain query
--  because supabase-py's query builder has no "order by vector distance"
--  method of its own; this is the standard Supabase+pgvector pattern.
-- ===========================================================================

create extension if not exists vector;

-- 768 dims: gemini-embedding-2's own recommended size (128-3072 range,
-- recommended 768/1536/3072) — plenty for a few dozen short FAQ entries,
-- and gemini-embedding-2 auto-normalizes output below 3072 dims so no
-- manual normalization step is needed before using cosine distance.
create table if not exists public.aqua_kb (
    id         bigint      generated always as identity primary key,
    title      text        not null,
    content    text        not null,
    embedding  vector(768),
    created_at timestamptz not null default now()
);

-- lists=10 is sized for a few dozen rows, not a large corpus — ivfflat's
-- own guidance is roughly sqrt(row_count) lists; re-tune if this table
-- ever grows past a few hundred rows. ANALYZE after bulk-loading rows
-- (tools/build_kb_embeddings.py's insert) so the planner has fresh stats.
create index if not exists aqua_kb_embedding_idx
    on public.aqua_kb using ivfflat (embedding vector_cosine_ops) with (lists = 10);

alter table public.aqua_kb enable row level security;

drop policy if exists aqua_kb_select_anon on public.aqua_kb;
create policy aqua_kb_select_anon on public.aqua_kb for select to anon using (true);
-- no insert/update/delete policy for anon — writes stay on service_role,
-- same read-only-for-anon shape as every other public table in this project.

create or replace function public.match_aqua_kb(query_embedding vector(768), match_count int default 5)
returns table (id bigint, title text, content text, similarity float)
language sql stable as $fn$
    select id, title, content, 1 - (embedding <=> query_embedding) as similarity
    from public.aqua_kb
    order by embedding <=> query_embedding
    limit match_count;
$fn$;

-- Explicit grant: don't rely on whatever this project's default privileges
-- happen to be for newly created functions — PostgREST calls this as
-- whichever role sb_read()'s key resolves to (anon), so it must be able to
-- EXECUTE it regardless.
grant execute on function public.match_aqua_kb(vector, int) to anon, authenticated, service_role;
