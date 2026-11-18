"""mcp_server.py — wraps ask.tools.AskTools as an MCP server, plus the
RequireApiKey ASGI gate in front of it. No real network; the one
integration-style test below drives a real MCP protocol handshake over an
in-process ASGI transport (httpx.ASGITransport) to lock in the exact
working pattern for this MCP SDK version — mounting/lifecycle here turned
out to have several non-obvious gotchas (see mcp_server.py's own docstring:
Host-header allowlisting, manual session lifecycle) that a pure-unit test
wouldn't catch if the SDK's behavior ever shifts again.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from ask.tools import TOOL_SCHEMAS, AskTools
from mcp_server import RequireApiKey, build_mcp_asgi_app, build_mcp_server, start_mcp_session, stop_mcp_session


def make_tools() -> AskTools:
    return AskTools(
        load_devices=lambda: [{"device_id": "esp32-aqua-01", "name": "Tank 1"}],
        load_history=lambda d, h: pd.DataFrame(),
        load_anomalies=lambda d, limit: pd.DataFrame(),
        load_forecasts=lambda d: pd.DataFrame(),
        load_all_latest=lambda: {
            "esp32-aqua-01": {
                "device_id": "esp32-aqua-01",
                "ts": "2026-01-01T00:00:00+00:00",
                "temperature": 25.0,
                "humidity": 60.0,
            }
        },
        search_kb=lambda query: [],
    )


# ---------------------------------------------------------------- build_mcp_server

def test_build_mcp_server_registers_every_tool_schema():
    server = build_mcp_server(make_tools())
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {s["name"] for s in TOOL_SCHEMAS}


def test_build_mcp_server_reuses_tool_schemas_descriptions():
    """Single source of truth check: the MCP-facing description for each
    tool must be exactly the one Gemini also sees (ask/tools.py's
    TOOL_SCHEMAS), not a second, independently-written copy that could
    drift from it."""
    server = build_mcp_server(make_tools())
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for schema in TOOL_SCHEMAS:
        assert tools[schema["name"]].description == schema["description"]


def test_build_mcp_server_tool_call_dispatches_to_the_real_method():
    """Not just registered — actually wired to the real AskTools instance,
    not a stub. get_latest's Asia/Taipei timestamp conversion appearing in
    the result proves this went through the real method body."""
    server = build_mcp_server(make_tools())
    result = asyncio.run(server.call_tool("get_latest", {"device_id": "esp32-aqua-01"}))
    assert result.is_error is False
    assert "08:00 (Asia/Taipei)" in result.content[0].text


def test_build_mcp_server_every_tool_is_marked_read_only():
    """Every one of these 7 tools is a pure Supabase read — tools/list must
    say so via the MCP spec's own annotation fields, not leave a client to
    guess whether a call is safe to repeat or could change something."""
    server = build_mcp_server(make_tools())
    tools = asyncio.run(server.list_tools())
    assert len(tools) == len(TOOL_SCHEMAS)
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False


# ------------------------------------------------------- isError on a known error

def test_known_errors_are_reported_as_is_error_with_the_original_message():
    """AskTools itself never raises (by design — see its own module
    docstring) — {"error": "..."} is a normal, successful Python return
    value that /ask's Gemini loop depends on unchanged. Only the MCP layer
    translates that into isError=True, and the content text must be
    exactly AskTools' own message, not the SDK's generic "Error executing
    tool <name>: ..." wrapper (see mcp_server.py's docstring for why
    raising an exception can't give you that)."""
    server = build_mcp_server(make_tools())

    r1 = asyncio.run(server.call_tool("get_latest", {"device_id": "no-such-device"}))
    assert r1.is_error is True
    assert r1.content[0].text == "unknown device_id 'no-such-device'; call list_devices for valid ids"

    r2 = asyncio.run(server.call_tool("get_stats", {"device_id": "esp32-aqua-01", "metric": "pressure", "hours": 24}))
    assert r2.is_error is True
    assert r2.content[0].text == "invalid metric 'pressure'; must be one of ['temperature', 'humidity']"


# ---------------------------------------------------------------- RequireApiKey

class _Recorder:
    """Minimal stand-in ASGI app: records whether it was ever called."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run_request(app, headers: list[tuple[bytes, bytes]]):
    scope = {"type": "http", "headers": headers}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status


def test_require_api_key_rejects_missing_header():
    inner = _Recorder()
    gated = RequireApiKey(inner, "secret123")
    status = _run_request(gated, [])
    assert status == 401
    assert inner.called is False


def test_require_api_key_rejects_wrong_key():
    inner = _Recorder()
    gated = RequireApiKey(inner, "secret123")
    status = _run_request(gated, [(b"authorization", b"Bearer wrong")])
    assert status == 401
    assert inner.called is False


def test_require_api_key_accepts_the_right_key():
    inner = _Recorder()
    gated = RequireApiKey(inner, "secret123")
    status = _run_request(gated, [(b"authorization", b"Bearer secret123")])
    assert status == 200
    assert inner.called is True


def test_require_api_key_passes_through_non_http_scopes_unchecked():
    """lifespan (startup/shutdown) events aren't HTTP requests and carry no
    Authorization header — must reach the inner app regardless."""
    inner = _Recorder()
    gated = RequireApiKey(inner, "secret123")

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(gated({"type": "lifespan"}, receive, send))
    assert inner.called is True


# ---------------------------------------------------------------- full round trip (integration)

def test_full_mcp_round_trip_over_http():
    """Real initialize -> tools/list -> tools/call -> wrong-key-rejected
    sequence over an in-process ASGI transport. This is the exact pattern
    mcp_server.py's own docstring describes as fragile (Host-header
    allowlisting, manual session lifecycle via start/stop_mcp_session) --
    a plain unit test wouldn't catch a regression in either of those."""
    httpx = pytest.importorskip("httpx")
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = build_mcp_server(make_tools())
    asgi_app = build_mcp_asgi_app(server, api_key="testkey123", allowed_hosts=["testserver"])
    root = Starlette(routes=[Mount("/mcp", app=asgi_app)])

    async def scenario():
        await start_mcp_session(server)
        try:
            transport = httpx.ASGITransport(app=root)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                headers = {
                    "authorization": "Bearer testkey123",
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                }
                r = await client.post(
                    "/mcp/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "1"},
                        },
                    },
                )
                assert r.status_code == 200
                headers["mcp-session-id"] = r.headers["mcp-session-id"]

                r2 = await client.post(
                    "/mcp/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                assert r2.status_code == 202

                r3 = await client.post(
                    "/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
                )
                assert r3.status_code == 200
                assert "get_latest" in r3.text

                r4 = await client.post(
                    "/mcp/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_latest", "arguments": {"device_id": "esp32-aqua-01"}},
                    },
                )
                assert r4.status_code == 200
                assert "25.0" in r4.text

                bad_headers = dict(headers)
                bad_headers["authorization"] = "Bearer wrong"
                r5 = await client.post(
                    "/mcp/", headers=bad_headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
                )
                assert r5.status_code == 401
        finally:
            await stop_mcp_session()

    asyncio.run(scenario())
