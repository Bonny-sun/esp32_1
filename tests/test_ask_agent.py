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
