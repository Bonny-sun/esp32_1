#!/usr/bin/env python3
"""
╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗

類別：MCP（Model Context Protocol）伺服器 ── 讓任何支援 MCP 的用戶端（Claude
      Desktop、Claude Code 等）能直接連進來查詢這套系統的資料，不透過 /ask
      頁面、不透過 Gemini。
目的：把 ask/tools.py 的 AskTools 那 7 個唯讀工具函式，原封不動包成 MCP 協定
      看得懂的工具——邏輯完全沒有重寫，只是多一層介面，所以這支檔案本身沒有
      任何商業邏輯，純粹是「轉接」。
資料流向：MCP 用戶端 ──(HTTP, Streamable HTTP transport)──> 【本程式】
          ──> ask.tools.AskTools（跟 /ask 頁面共用同一份邏輯）──> Supabase

跟 /ask 的差異：/ask 是「使用者打字問問題 → Gemini 決定呼叫哪個工具」；這支
檔案是「MCP 用戶端自己的 LLM（例如使用者自己的 Claude）決定呼叫哪個工具」——
兩條路徑最後都走到同一組 AskTools 方法，資料來源保證一致。

程式架構：
  一、驗證          【1-1】RequireApiKey ASGI 中介層
  二、建立 MCP 伺服器 【2-1a】_wrap_as_mcp_tool() 錯誤轉 isError
                     【2-1b】build_mcp_server() 把 AskTools 包成 MCP 工具
  三、掛載與生命週期   【3-1】build_mcp_asgi_app()  【3-2】session 啟動/關閉

備註：
  為什麼不用 MCP 規格建議的 OAuth ...... 這是個人作品集展示用的唯讀資料
                                         服務，不是多租戶正式產品；固定
                                         API Key（跟 GEMINI_API_KEY、
                                         DASH_PASSWORD_HASH 同樣的環境變數
                                         密鑰模式）已經足夠，OAuth 的額外
                                         基礎設施在這裡不成比例
  為什麼需要手動管理 session 生命週期 ... streamable_http_app() 回傳的是
                                         獨立的 Starlette 子應用，用
                                         app.mount() 掛上去時，Starlette
                                         不會自動把子應用的 lifespan
                                         併入父應用——親自驗證過，不做這步
                                         每個真實請求都會是 500
                                         "Task group is not initialized"
  改能呼叫哪些工具 ...................... 不用改這支檔案，改 ask/tools.py
                                         的 TOOL_SCHEMAS 跟 AskTools 本身，
                                         這支檔案只是依照 TOOL_SCHEMAS 的
                                         名稱/說明文字自動註冊
  為什麼錯誤要在這裡轉成 isError=True ... AskTools 的方法設計成「不丟例外,
                                         失敗回傳 {"error": ...}」(給 /ask
                                         的 Gemini 迴圈用),但 MCP 協定有
                                         自己的 isError 欄位讓用戶端能明確
                                         分辨「查到資料」跟「查詢失敗」。
                                         這裡只在 MCP 這一層把 {"error":
                                         ...} 轉成 isError=True,AskTools
                                         本身完全不動,/ask 拿到的還是原本
                                         的 {"error": ...} dict
  為什麼不直接 raise 例外來設 isError .... 試過 — MCP SDK 會把例外訊息包成
                                         "Error executing tool <name>:
                                         <msg>",不是原始錯誤訊息本身。
                                         改成直接回傳 CallToolResult(
                                         isError=True, content=[...]),
                                         才能讓 content 文字就是 AskTools
                                         原本的錯誤字串,不多包一層

╚════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from ask.tools import TOOL_SCHEMAS, AskTools
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# Every AskTools method is a pure Supabase read with a fixed, small set of
# possible calls — none of them write anything, repeat-calling one is
# always safe, and none of them reach outside this system's own data.
# Verified against the MCP spec's actual field names (readOnlyHint/
# destructiveHint/idempotentHint/openWorldHint on the wire) before writing
# this, not assumed from memory.
_READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# ============================================================================
# 一、驗證：固定 API Key，比對 Authorization header
# ============================================================================


# ---- 【1-1】ASGI 中介層：Authorization 不是 "Bearer <api_key>" 就直接 401,不放行到 MCP 伺服器 ----
class RequireApiKey:
    """Plain ASGI middleware wrapping the MCP sub-app. Rejects any HTTP
    request whose `Authorization` header isn't exactly `Bearer <api_key>` —
    same env-var-secret pattern as this project's other credentials
    (`GEMINI_API_KEY`, `DASH_PASSWORD_HASH`), not MCP's own OAuth plumbing
    (see this module's own docstring for why)."""

    def __init__(self, app: "ASGIApp", api_key: str) -> None:
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        got = headers.get(b"authorization", b"").decode()
        if got != f"Bearer {self._api_key}":
            response = PlainTextResponse("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


# ============================================================================
# 二、建立 MCP 伺服器：把 AskTools 的每個方法註冊成一個 MCP 工具
# ============================================================================


# ---- 【2-1a】把 AskTools 方法包一層：回傳 dict 有 "error" 就轉成 isError=True,其餘原封不動轉交 ----
def _wrap_as_mcp_tool(method: Any) -> Any:
    """Wraps a bound AskTools method so a `{"error": ...}` result becomes an
    MCP `CallToolResult(isError=True, ...)` whose content is exactly that
    error string — not AskTools itself, only how the MCP layer presents its
    result (see this module's own docstring for why `raise` can't do this:
    the SDK always prefixes a raised message with "Error executing tool
    <name>: ").

    `functools.wraps(method)` matters here beyond cosmetics: the MCP SDK
    builds each tool's input JSON schema from `inspect.signature(fn)`, and
    `inspect.signature` follows a wrapper's `__wrapped__` attribute (which
    `wraps` sets) back to the original method — so the schema the client
    sees still matches `device_id`/`metric`/`hours`/etc. exactly, not a
    bare `(*args, **kwargs)`. Verified by actually calling `list_tools()`
    against a wrapped method before relying on this, not assumed."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = method(*args, **kwargs)
        if isinstance(result, dict) and "error" in result:
            return CallToolResult(content=[TextContent(type="text", text=result["error"])], isError=True)
        return result

    return wrapper


# ---- 【2-1b】依照 TOOL_SCHEMAS 的名稱/說明文字,把 tools 身上對應的方法逐一註冊進去 ----
def build_mcp_server(tools: AskTools) -> MCPServer:
    """Register every AskTools method named in TOOL_SCHEMAS as an MCP tool.
    Reuses TOOL_SCHEMAS' own name/description strings (the same ones
    ask/agent.py hands to Gemini) as the single source of truth, rather than
    maintaining a second, possibly-drifting description here. The JSON
    input schema itself comes from each method's own Python type hints —
    the MCP SDK builds it automatically, same as it already does for the
    `@server.tool()` decorator form. Every tool gets the same read-only
    annotations (`_READ_ONLY_TOOL_ANNOTATIONS`) — true of all 7 alike."""
    server = MCPServer(name="aiot-aquaponics", version="1.0")
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        server.add_tool(
            _wrap_as_mcp_tool(getattr(tools, name)),
            name=name,
            description=schema["description"],
            annotations=_READ_ONLY_TOOL_ANNOTATIONS,
        )
    return server


# ============================================================================
# 三、掛載與生命週期
# ============================================================================


# ---- 【3-1】組成掛在 /mcp 路徑下的 ASGI 應用：Host header 白名單 + API Key 驗證 ----
def build_mcp_asgi_app(server: MCPServer, *, api_key: str, allowed_hosts: list[str]) -> "ASGIApp":
    """`allowed_hosts` guards against DNS-rebinding (MCP's own transport
    security feature) — every real request's Host header must be in this
    list or the MCP SDK itself rejects it with 421, independent of the API
    key check above. `allowed_origins=["*"]`: MCP clients are typically not
    browsers, so Origin-based CORS protection buys little here — the API
    key is this endpoint's real access control."""
    security = TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=["*"])
    inner = server.streamable_http_app(streamable_http_path="/", transport_security=security)
    return RequireApiKey(inner, api_key)


# ---- 【3-2】手動管理 session 的啟動/關閉(見本檔案開頭備註,為什麼需要這麼做) ----
_session_cm = None


async def start_mcp_session(server: MCPServer) -> None:
    """Call once from the hosting app's startup hook (e.g. NiceGUI's
    `app.on_startup`). Must be paired with `stop_mcp_session()` on shutdown."""
    global _session_cm
    _session_cm = server.session_manager.run()
    await _session_cm.__aenter__()


async def stop_mcp_session() -> None:
    global _session_cm
    if _session_cm is not None:
        await _session_cm.__aexit__(None, None, None)
        _session_cm = None
