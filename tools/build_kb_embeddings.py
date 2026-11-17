#!/usr/bin/env python3
"""
One-off (offline, run-by-hand) script: embeds every entry in
tools/kb_content.py via the Gemini Embedding API and (re)writes them into
Supabase's aqua_kb table (sql/11_knowledge_base.sql), so /ask's
search_manual tool can do RAG retrieval over them.

Not a service, not imported by the deployed app — dashboard/app.py never
needs the FAQ source text at runtime, only the embeddings already sitting
in aqua_kb. Re-run this only when tools/kb_content.py's content changes.

Before the first run: paste sql/11_knowledge_base.sql into the Supabase SQL
editor (creates the pgvector extension, aqua_kb table, and match_aqua_kb
function).

Usage:
    cd dashboard && python3 -m pip install -r requirements.txt  # if not already
    cd .. && python3 tools/build_kb_embeddings.py

Needs the same env vars dashboard/.env already has (loaded from there
directly, so nothing new to configure):
    GEMINI_API_KEY        - Google AI Studio key
    SUPABASE_URL          - Supabase project URL
    SUPABASE_SERVICE_KEY  - service_role key (writes bypass aqua_kb's
                             anon-read-only RLS policy on purpose)

Embedding dimension (EMBEDDING_DIM below) must match agent.py's own
EMBEDDING_DIM and sql/11_knowledge_base.sql's vector(768) column — all
three are meant to move together if ever changed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "dashboard" / ".env")

sys.path.insert(0, str(ROOT / "tools"))
from kb_content import KB_ENTRIES  # noqa: E402 - must follow sys.path.insert

EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIM = 768


def main() -> None:
    from google import genai
    from google.genai import types
    from supabase import create_client

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    print(f"[kb] clearing existing aqua_kb rows ({len(KB_ENTRIES)} entries to rebuild)...")
    sb.table("aqua_kb").delete().gte("id", 0).execute()

    for entry in KB_ENTRIES:
        # Document-side asymmetric-retrieval prefix, per Gemini's own docs
        # for gemini-embedding-2 (no task_type config field — the task has
        # to be written into the text itself). app.py's search_kb() uses
        # the matching query-side prefix ("task: search result | query:
        # ..."); the two must agree or retrieval quality silently degrades.
        text = f"title: {entry['title']} | text: {entry['content']}"
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
        )
        vector = list(result.embeddings[0].values)
        sb.table("aqua_kb").insert(
            {"title": entry["title"], "content": entry["content"], "embedding": vector}
        ).execute()
        print(f"[kb] embedded: {entry['title']}")

    print(f"[kb] done — {len(KB_ENTRIES)} entries embedded into aqua_kb.")


if __name__ == "__main__":
    main()
