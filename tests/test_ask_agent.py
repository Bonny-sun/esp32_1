"""ask.agent — answer_question() driven with a scripted fake LLMClient (no
google-genai, no network, no API key) plus RateLimiter on its own clock."""
from __future__ import annotations

import pandas as pd
import pytest
from ask.agent import GeminiLLMClient, ModelTurn, RateLimitConfig, RateLimiter, ToolCall, answer_question
from ask.tools import TOOL_SCHEMAS, AskTools


def make_tools() -> AskTools:
    devices = [{"device_id": "esp32-aqua-01", "name": "Tank 1"}]
    latest = {
        "esp32-aqua-01": {
            "device_id": "esp32-aqua-01",
            "ts": "2026-01-01T06:00:00+00:00",
            "temperature": 25.0,
            "humidity": 60.0,
        }
    }
    return AskTools(
        load_devices=lambda: devices,
        load_history=lambda d, h: pd.DataFrame(),
        load_anomalies=lambda d, limit: pd.DataFrame(),
        load_forecasts=lambda d: pd.DataFrame(),
        load_all_latest=lambda: latest,
    )


class ScriptedLLM:
    """Returns each entry in `turns` in order, one per generate() call;
    records every `messages` list it was called with so a test can inspect
    what the agent loop actually fed back after a tool ran."""

    def __init__(self, turns: list[ModelTurn]):
        self.turns = list(turns)
        self.calls: list[list[dict]] = []

    def generate(self, messages: list[dict]) -> ModelTurn:
        self.calls.append([dict(m) for m in messages])
        return self.turns.pop(0)


class BoomLLM:
    def generate(self, messages):  # pragma: no cover - should never be called
        raise AssertionError("should not be called")


def test_direct_text_answer_no_tools_needed():
    llm = ScriptedLLM([ModelTurn(text="你好")])
    out = answer_question("哈囉", llm=llm, tools=make_tools())
    assert out == "你好"
    assert len(llm.calls) == 1


def test_calls_tool_then_answers():
    llm = ScriptedLLM(
        [
            ModelTurn(tool_calls=[ToolCall(name="get_latest", args={"device_id": "esp32-aqua-01"})]),
            ModelTurn(text="最新溫度是 25.0°C"),
        ]
    )
    out = answer_question("最新溫度?", llm=llm, tools=make_tools())
    assert out == "最新溫度是 25.0°C"
    assert len(llm.calls) == 2
    # second call's history carries the model's tool-call turn + the tool's result
    second_call_messages = llm.calls[1]
    assert second_call_messages[1]["role"] == "model"
    assert second_call_messages[1]["tool_calls"][0]["name"] == "get_latest"
    assert second_call_messages[2]["role"] == "tool"
    assert second_call_messages[2]["result"]["reading"]["temperature"] == 25.0


def test_stops_at_max_rounds_with_fixed_message():
    # Always asks for another tool call — never gives a final answer.
    forever = ScriptedLLM([ModelTurn(tool_calls=[ToolCall(name="list_devices", args={})]) for _ in range(10)])
    out = answer_question("随便问", llm=forever, tools=make_tools(), max_rounds=4)
    assert "無法在限定的查詢次數內完成" in out
    assert len(forever.calls) == 4  # exactly max_rounds generate() calls, not 5+


def test_unknown_tool_name_does_not_crash_the_loop():
    llm = ScriptedLLM(
        [
            ModelTurn(tool_calls=[ToolCall(name="drop_table", args={})]),
            ModelTurn(text="抱歉，我不能執行那個操作"),
        ]
    )
    out = answer_question("幫我清空資料庫", llm=llm, tools=make_tools())
    assert out == "抱歉，我不能執行那個操作"
    tool_result = llm.calls[1][2]["result"]
    assert "error" in tool_result


def test_tool_raising_exception_is_reported_not_propagated():
    def boom_loader():
        raise RuntimeError("supabase is down")

    tools = AskTools(
        load_devices=lambda: [{"device_id": "esp32-aqua-01", "name": None}],
        load_history=lambda d, h: pd.DataFrame(),
        load_anomalies=lambda d, limit: pd.DataFrame(),
        load_forecasts=lambda d: pd.DataFrame(),
        load_all_latest=boom_loader,
    )
    llm = ScriptedLLM(
        [
            ModelTurn(tool_calls=[ToolCall(name="get_latest", args={})]),
            ModelTurn(text="查詢時發生問題"),
        ]
    )
    out = answer_question("最新溫度?", llm=llm, tools=tools)
    assert out == "查詢時發生問題"
    assert "error" in llm.calls[1][2]["result"]


def test_empty_turn_falls_back_to_generic_message():
    llm = ScriptedLLM([ModelTurn()])  # neither tool_calls nor text
    out = answer_question("?", llm=llm, tools=make_tools())
    assert out == "很抱歉,目前無法產生回答,請換個方式提問。"


def test_question_is_truncated_to_max_chars():
    llm = ScriptedLLM([ModelTurn(text="ok")])
    long_question = "問" * 1000
    answer_question(long_question, llm=llm, tools=make_tools())
    sent = llm.calls[0][0]["text"]
    assert len(sent) == 300


# ---------------------------------------------------------------- GeminiLLMClient
# No network call here — just confirms the real google-genai SDK accepts
# every TOOL_SCHEMAS entry's parameters_json_schema and builds a valid
# GenerateContentConfig, catching an SDK-shape mismatch (e.g. a typo'd
# schema key) without needing a real API key or a live call.

def test_gemini_client_builds_tool_declarations_from_all_schemas():
    client = GeminiLLMClient(api_key="unit-test-key-not-a-real-credential", model="gemini-flash-latest")
    declared = client._config.tools[0].function_declarations
    assert {d.name for d in declared} == {s["name"] for s in TOOL_SCHEMAS}


def test_answer_question_threads_raw_through_to_the_model_message():
    """The agent loop doesn't interpret ModelTurn.raw — it just has to carry
    it, unchanged, onto the resulting "model" message's "raw" key so an
    LLMClient can get it back on its next generate() call."""
    sentinel = object()
    llm = ScriptedLLM(
        [
            ModelTurn(tool_calls=[ToolCall(name="list_devices", args={})], raw=sentinel),
            ModelTurn(text="ok"),
        ]
    )
    answer_question("?", llm=llm, tools=make_tools())
    assert llm.calls[1][1]["raw"] is sentinel


def test_gemini_to_content_resends_the_original_model_content_unchanged():
    """Regression test for a live bug: the Gemini API rejects a resent
    function-call turn that's missing the original thought_signature
    (400 INVALID_ARGUMENT, "Function call is missing a thought_signature")
    if the turn was rebuilt from scratch (Part.from_function_call only
    carries name/args, never the signature the model originally attached).
    GeminiLLMClient must resend response.candidates[0].content byte-for-byte
    via the "raw" key instead of reconstructing an equivalent-looking Part."""
    client = GeminiLLMClient(api_key="unit-test-key-not-a-real-credential", model="gemini-flash-latest")
    types = client._types
    original_part = types.Part.from_function_call(name="get_latest", args={"device_id": "esp32-aqua-01"})
    original_part.thought_signature = b"opaque-signature-bytes"
    original_content = types.Content(role="model", parts=[original_part])

    message = {
        "role": "model",
        "tool_calls": [{"name": "get_latest", "args": {"device_id": "esp32-aqua-01"}}],
        "raw": original_content,
    }
    resent = client._to_content(message)

    assert resent is original_content
    assert resent.parts[0].thought_signature == b"opaque-signature-bytes"


def test_gemini_to_content_falls_back_when_raw_is_missing():
    client = GeminiLLMClient(api_key="unit-test-key-not-a-real-credential", model="gemini-flash-latest")
    message = {"role": "model", "tool_calls": [{"name": "list_devices", "args": {}}], "raw": None}
    resent = client._to_content(message)
    assert resent.parts[0].function_call.name == "list_devices"
    assert resent.parts[0].thought_signature is None


# ---------------------------------------------------------- generate() retry
# Regression coverage for the real incident (PRs #146/#147): the API spec
# promised a retry alongside the timeout, but only the timeout was ever
# wired up. These drive _generate_with_retry() directly against a scripted
# fake of the google-genai call, with a captured (not real) sleep, so a
# retry round-trips in milliseconds rather than actually waiting.

def _client_with_fake_call(monkeypatch, side_effects):
    """A GeminiLLMClient whose underlying SDK call is replaced by a
    canned sequence of return values / exceptions, and whose backoff sleep
    is captured instead of actually executed."""
    sleeps: list[float] = []
    client = GeminiLLMClient(
        api_key="unit-test-key-not-a-real-credential",
        model="gemini-flash-latest",
        sleep=sleeps.append,
    )
    calls = {"n": 0}

    def fake_generate_content(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        effect = side_effects[i]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    monkeypatch.setattr(client._client.models, "generate_content", fake_generate_content)
    return client, calls, sleeps


def _fake_response(text="ok"):
    import types

    return types.SimpleNamespace(function_calls=None, text=text, candidates=None)


def test_generate_retries_a_timeout_then_succeeds(monkeypatch):
    import httpx

    client, calls, sleeps = _client_with_fake_call(
        monkeypatch, [httpx.ReadTimeout("timed out"), _fake_response("recovered")]
    )
    turn = client.generate([{"role": "user", "text": "hi"}])
    assert turn.text == "recovered"
    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_generate_retries_a_server_error_then_succeeds(monkeypatch):
    from google.genai import errors as genai_errors

    server_error = genai_errors.ServerError(503, {"error": {"message": "unavailable"}})
    client, calls, sleeps = _client_with_fake_call(monkeypatch, [server_error, _fake_response("recovered")])
    turn = client.generate([{"role": "user", "text": "hi"}])
    assert turn.text == "recovered"
    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_generate_gives_up_after_max_retries(monkeypatch):
    import httpx

    timeouts = [httpx.ReadTimeout("timed out")] * 10  # more than enough attempts
    client, calls, sleeps = _client_with_fake_call(monkeypatch, timeouts)
    with pytest.raises(httpx.ReadTimeout):
        client.generate([{"role": "user", "text": "hi"}])
    # MAX_GENERATE_RETRIES extra attempts beyond the first = 1 + MAX_GENERATE_RETRIES total
    from ask.agent import MAX_GENERATE_RETRIES

    assert calls["n"] == MAX_GENERATE_RETRIES + 1
    assert len(sleeps) == MAX_GENERATE_RETRIES


def test_generate_does_not_retry_a_client_error(monkeypatch):
    from google.genai import errors as genai_errors

    client_error = genai_errors.ClientError(400, {"error": {"message": "invalid argument"}})
    client, calls, sleeps = _client_with_fake_call(monkeypatch, [client_error, _fake_response("unused")])
    with pytest.raises(genai_errors.ClientError):
        client.generate([{"role": "user", "text": "hi"}])
    assert calls["n"] == 1
    assert sleeps == []


# ---------------------------------------------------------------- RateLimiter

def test_rate_limiter_allows_then_blocks_per_minute():
    clock = {"t": 0.0}
    limiter = RateLimiter(RateLimitConfig(per_minute=2, per_day=100, site_per_day=100), now=lambda: clock["t"])
    assert limiter.check("u1") is None
    limiter.record("u1")
    assert limiter.check("u1") is None
    limiter.record("u1")
    reason = limiter.check("u1")
    assert reason is not None and "每分鐘" in reason


def test_rate_limiter_per_minute_window_expires():
    clock = {"t": 0.0}
    limiter = RateLimiter(RateLimitConfig(per_minute=1, per_day=100, site_per_day=100), now=lambda: clock["t"])
    limiter.record("u1")
    assert limiter.check("u1") is not None
    clock["t"] += 61  # past the 60s window
    assert limiter.check("u1") is None


def test_rate_limiter_per_day_cap_independent_per_identity():
    clock = {"t": 0.0}
    limiter = RateLimiter(RateLimitConfig(per_minute=100, per_day=1, site_per_day=100), now=lambda: clock["t"])
    limiter.record("u1")
    assert limiter.check("u1") is not None
    assert limiter.check("u2") is None  # different identity, untouched


def test_rate_limiter_site_wide_cap_blocks_everyone():
    clock = {"t": 0.0}
    limiter = RateLimiter(RateLimitConfig(per_minute=100, per_day=100, site_per_day=1), now=lambda: clock["t"])
    limiter.record("u1")
    reason = limiter.check("u2")
    assert reason is not None and "全站" in reason


@pytest.mark.parametrize(
    "env_key, attr",
    [
        ("ASK_RATE_PER_MINUTE", "per_minute"),
        ("ASK_RATE_PER_DAY", "per_day"),
        ("ASK_RATE_SITE_PER_DAY", "site_per_day"),
    ],
)
def test_rate_limit_config_reads_env_vars(monkeypatch, env_key, attr):
    monkeypatch.setenv(env_key, "42")
    assert getattr(RateLimitConfig(), attr) == 42
