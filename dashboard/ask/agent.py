"""The /ask page's LLM-agnostic agent loop, a Gemini implementation of it,
and an in-memory abuse/cost guard.

`LLMClient` is the seam: app.py builds a real `GeminiLLMClient`, tests build
a scripted fake and drive `answer_question()` directly — nothing outside
this module's `GeminiLLMClient` imports `google.genai`, so the rest of the
agent loop (round counting, tool dispatch, error handling) is testable
without the SDK, a network call, or an API key.

Message shape passed between the agent loop and an `LLMClient` is a plain
list of dicts — not google-genai's own `Content`/`Part` types — precisely so
a fake LLMClient in tests doesn't need that SDK installed to participate:
    {"role": "user", "text": "..."}
    {"role": "model", "tool_calls": [{"name": "...", "args": {...}}, ...], "raw": <opaque, optional>}
    {"role": "tool", "name": "...", "result": {...}}

"raw" on a "model" message is an escape hatch for an LLMClient implementation
that needs to resend its own previous turn byte-for-byte rather than have the
agent loop reconstruct an equivalent-looking one — see GeminiLLMClient's
thought_signature handling below for why that distinction matters. The
agent loop itself never reads or interprets "raw"; it just carries whatever
the LLMClient that produced it put there back to that same LLMClient.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ask.tools import TOOL_SCHEMAS, AskTools

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
MAX_QUESTION_CHARS = 300
DEFAULT_MODEL = "gemini-flash-latest"  # a Google-maintained alias for the
# current recommended Flash model, not a dated version — the model landscape
# moves fast enough that pinning an exact name here would go stale; GEMINI_MODEL
# always wins when set, this is only the fallback if it's unset.
DEFAULT_MAX_OUTPUT_TOKENS = 1024
# Per-call HTTP timeout for a single generate_content request (one round of
# the agent loop, not the whole question). 20s was too tight in practice —
# a thinking-enabled model can legitimately take longer than that on a
# question needing real reasoning (e.g. grouping/counting tool results by
# device+metric), not just a slow network — confirmed live via Render logs:
# "The read operation timed out" on exactly that kind of question.
DEFAULT_TIMEOUT_MS = 60_000
# How many extra attempts a single agent-loop round gets if the Gemini call
# itself fails transiently (a timeout, or the API returning 5xx) — "重試機制"
# alongside the timeout above. 2 retries (3 attempts total) rather than more:
# each one costs up to DEFAULT_TIMEOUT_MS of real wait, and this already runs
# inside one round of answer_question's own MAX_TOOL_ROUNDS backstop, so
# piling on retries here risks the whole request running long rather than
# actually recovering. Not applied to a 4xx (ClientError) — a bad request
# (e.g. the missing-thought_signature bug) fails identically on every retry,
# so retrying it only burns quota for no chance of success.
MAX_GENERATE_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.0

SYSTEM_PROMPT = (
    "你是一個 AIoT 溫溼度監測系統的資料助理。你只能透過提供的工具查詢資料,"
    "絕對不能自己編造數值或引用工具結果以外的資訊。如果工具回傳查無資料,"
    "就明確告訴使用者「查無資料」,不要用猜測的數字回答。"
    "只回答跟這個系統的感測資料、異常警戒、AI預測有關的問題;"
    "跟系統資料無關的問題,禮貌說明你只能回答系統相關的問題並拒絕回答。"
    "回答一律使用繁體中文,簡短清楚,並在最後附上一行「資料來源」,"
    "簡述你查詢了哪個工具、哪個裝置、哪段時間範圍,方便使用者自行驗證。"
)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ModelTurn:
    """One LLM response: either tool calls to make next, or a final answer.
    Exactly one of the two is populated — `text` means "done".

    `raw` is opaque, implementation-specific state an LLMClient can stash
    here to get back unchanged on its next `generate()` call via the
    resulting "model" message's own "raw" key — see this module's docstring.
    Unused by the agent loop itself and by LLMClient implementations that
    don't need it (a fake test LLM never sets or reads it)."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str | None = None
    raw: Any = None


class LLMClient(Protocol):
    def generate(self, messages: list[dict]) -> ModelTurn: ...


def _execute_tool(tools: AskTools, name: str, args: dict) -> dict:
    """Never raises — any failure (bad tool name, the underlying loader
    blowing up) comes back as {"error": ...} so the agent loop can hand it
    to the model as a normal tool result and let the model react ("無法查詢
    該資料") instead of the whole request crashing."""
    method = getattr(tools, name, None)
    if method is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return method(**args)
    except Exception as exc:  # noqa: BLE001 - last line of defense around arbitrary tool args
        logger.warning("ask tool %s failed: %s", name, exc)
        return {"error": f"tool {name} failed: {exc}"}


def answer_question(question: str, *, llm: LLMClient, tools: AskTools, max_rounds: int = MAX_TOOL_ROUNDS) -> str:
    """Drives the tool-calling loop to a final text answer, or a fixed
    give-up message if the model never stops asking for tools within
    `max_rounds` — this is the cost/latency backstop the spec calls for, a
    stuck model can cost at most `max_rounds` tool calls plus `max_rounds`
    LLM calls per question, never an unbounded loop."""
    question = question.strip()[:MAX_QUESTION_CHARS]
    messages: list[dict] = [{"role": "user", "text": question}]
    for _ in range(max_rounds):
        turn = llm.generate(messages)
        if not turn.tool_calls:
            return turn.text or "很抱歉,目前無法產生回答,請換個方式提問。"
        messages.append(
            {
                "role": "model",
                "tool_calls": [{"name": tc.name, "args": tc.args} for tc in turn.tool_calls],
                "raw": turn.raw,
            }
        )
        for tc in turn.tool_calls:
            result = _execute_tool(tools, tc.name, tc.args)
            messages.append({"role": "tool", "name": tc.name, "result": result})
    return "無法在限定的查詢次數內完成這個問題,請換個方式提問或縮小問題範圍。"


class GeminiLLMClient:
    """Real LLMClient backed by Google's `google-genai` SDK, manual
    (non-automatic) function calling — automatic mode's own built-in loop
    caps at 10 remote calls and isn't driven by our `messages` log, so it
    can't give us the "exactly 4 rounds, our own give-up message" behavior
    the spec asks for. Builds its own Tool/FunctionDeclaration from
    ask.tools.TOOL_SCHEMAS (plain JSON Schema via parameters_json_schema,
    not hand-built google.genai.types.Schema objects)."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # All imported lazily: nothing else in this file, and nothing in
        # tools.py, needs google-genai or httpx importable — keeps a
        # fake-LLM test run (and any future caller with no key configured)
        # from needing the real SDK on the path at all.
        import httpx
        from google import genai
        from google.genai import errors as genai_errors

        self._types = genai.types
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._sleep = sleep
        self._retryable_errors = (httpx.TimeoutException, genai_errors.ServerError)
        decls = [
            self._types.FunctionDeclaration(
                name=s["name"], description=s["description"], parameters_json_schema=s["parameters"]
            )
            for s in TOOL_SCHEMAS
        ]
        self._config = self._types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[self._types.Tool(function_declarations=decls)],
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=max_output_tokens,
            http_options=self._types.HttpOptions(timeout=timeout_ms),
        )

    def generate(self, messages: list[dict]) -> ModelTurn:
        contents = [self._to_content(m) for m in messages]
        response = self._generate_with_retry(contents)
        calls = response.function_calls or []
        if calls:
            # Keep the model's own Content object (not just name/args) so it
            # can be resent byte-for-byte next round — see _to_content's
            # "model" branch and this module's docstring. A model with
            # "thinking" enabled attaches a thought_signature to each
            # function_call Part; rebuilding a fresh Part from only name+args
            # drops that signature, which the API then rejects with
            # INVALID_ARGUMENT ("Function call is missing a
            # thought_signature") on the very next turn.
            raw = response.candidates[0].content
            return ModelTurn(tool_calls=[ToolCall(name=c.name, args=dict(c.args or {})) for c in calls], raw=raw)
        return ModelTurn(text=(response.text or "").strip() or None)

    def _generate_with_retry(self, contents):
        """Retry a transient failure (timeout, 5xx) up to MAX_GENERATE_RETRIES
        extra times with a short fixed backoff between attempts; a non-
        transient failure (4xx) is raised immediately, unretried."""
        for attempt in range(MAX_GENERATE_RETRIES + 1):
            try:
                return self._client.models.generate_content(
                    model=self._model, contents=contents, config=self._config
                )
            except self._retryable_errors as exc:
                if attempt >= MAX_GENERATE_RETRIES:
                    raise
                logger.warning(
                    "ask: Gemini call failed (%s), retrying (%d/%d)",
                    exc, attempt + 1, MAX_GENERATE_RETRIES,
                )
                self._sleep(RETRY_BACKOFF_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover

    def _to_content(self, message: dict):
        types = self._types
        role = message["role"]
        if role == "user":
            return types.Content(role="user", parts=[types.Part.from_text(text=message["text"])])
        if role == "model":
            raw = message.get("raw")
            if raw is not None:
                return raw
            # Fallback for a hypothetical non-Gemini-produced "model" message
            # (never hit in practice — GeminiLLMClient is the only producer
            # of "model" messages in this codebase, and it always sets
            # "raw"). Kept rather than asserting, since losing
            # thought_signature here would be a silent quality regression,
            # not a crash, for any future caller that reaches it.
            parts = [types.Part.from_function_call(name=tc["name"], args=tc["args"]) for tc in message["tool_calls"]]
            return types.Content(role="model", parts=parts)
        if role == "tool":
            part = types.Part.from_function_response(name=message["name"], response={"result": message["result"]})
            return types.Content(role="user", parts=[part])
        raise ValueError(f"unknown message role {role!r}")


# ---------------------------------------------------------------- rate limit
# Cost/abuse guard for a public demo site: caps how often one visitor (and
# the site as a whole) can trigger a real LLM call. Deliberately a plain
# in-process dict, not a DB table — this only needs to survive one Render
# instance's uptime, not a restart, and avoids spending a Supabase round
# trip on every single /ask keystroke's worth of protection. Resets to zero
# on every deploy/restart (documented in README) — an accepted tradeoff for
# a single-instance low-traffic demo, not something a real multi-tenant
# product would do.
@dataclass
class RateLimitConfig:
    per_minute: int = field(default_factory=lambda: int(os.environ.get("ASK_RATE_PER_MINUTE", "6")))
    per_day: int = field(default_factory=lambda: int(os.environ.get("ASK_RATE_PER_DAY", "60")))
    site_per_day: int = field(default_factory=lambda: int(os.environ.get("ASK_RATE_SITE_PER_DAY", "500")))


class RateLimiter:
    def __init__(self, config: RateLimitConfig | None = None, *, now: Callable[[], float] = time.time) -> None:
        self._config = config or RateLimitConfig()
        self._now = now
        self._per_identity: dict[str, list[float]] = {}
        self._site: list[float] = []

    @staticmethod
    def _prune(timestamps: list[float], cutoff: float) -> list[float]:
        return [t for t in timestamps if t > cutoff]

    def check(self, identity: str) -> str | None:
        """None = allowed; otherwise a ready-to-show 繁體中文 reason."""
        now = self._now()
        history = self._prune(self._per_identity.get(identity, []), now - 86400)
        if len([t for t in history if t > now - 60]) >= self._config.per_minute:
            return "問太快了,請稍等一下再問一次（每分鐘有次數限制）。"
        if len(history) >= self._config.per_day:
            return "今天您的提問次數已達上限,請明天再來。"
        if len(self._prune(self._site, now - 86400)) >= self._config.site_per_day:
            return "今天全站的提問次數已達上限,請明天再來。"
        return None

    def record(self, identity: str) -> None:
        now = self._now()
        pruned = self._prune(self._per_identity.get(identity, []), now - 86400)
        pruned.append(now)
        self._per_identity[identity] = pruned
        self._site = self._prune(self._site, now - 86400)
        self._site.append(now)
