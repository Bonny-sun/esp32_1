"""ask.tools.AskTools — all loaders are fakes; no Supabase, no network.

Fakes mirror the exact shapes dashboard/app.py's real load_*() functions
return (see their docstrings): load_history's index is tz-naive Asia/Taipei
wall-clock with either raw temperature/humidity columns (<=48h) or
_avg/_min/_max columns (>48h, the hourly rollup table); load_anomalies/
load_forecasts return tz-aware (Asia/Taipei) pandas Timestamps;
load_all_latest returns raw (un-converted, UTC) ISO timestamp strings.
"""
from __future__ import annotations

import pandas as pd
import pytest
from ask.tools import AskTools

DEVICES = [
    {"device_id": "esp32-aqua-01", "name": "Tank 1"},
    {"device_id": "esp32-aqua-02", "name": None},
]


def make_tools(*, history=None, anomalies=None, forecasts=None, latest=None) -> AskTools:
    return AskTools(
        load_devices=lambda: DEVICES,
        load_history=history or (lambda device_id, hours: pd.DataFrame()),
        load_anomalies=anomalies or (lambda device_id, limit: pd.DataFrame()),
        load_forecasts=forecasts or (lambda device_id: pd.DataFrame()),
        load_all_latest=latest or (lambda: {}),
    )


# ---------------------------------------------------------------- list_devices

def test_list_devices_falls_back_name_to_id():
    tools = make_tools()
    out = tools.list_devices()
    assert out["count"] == 2
    assert out["devices"][1]["name"] == "esp32-aqua-02"  # name was None


# ---------------------------------------------------------------- validation

def test_unknown_device_returns_error_not_exception():
    tools = make_tools()
    assert "error" in tools.get_latest(device_id="nope")
    assert "error" in tools.get_stats("nope", "temperature", 24)
    assert "error" in tools.get_anomalies(device_id="nope")
    assert "error" in tools.get_forecast("nope")


def test_invalid_metric_rejected():
    tools = make_tools(history=lambda d, h: pd.DataFrame({"temperature": [1.0]}, index=[pd.Timestamp("2026-01-01")]))
    out = tools.get_stats("esp32-aqua-01", "pressure", 24)
    assert "error" in out
    assert "pressure" in out["error"]


@pytest.mark.parametrize(
    "hours_in, expected",
    [(0, 1), (-5, 1), (9999, 168), (24, 24), (1, 1)],
)
def test_hours_clamped_into_1_to_168(hours_in, expected):
    captured = {}

    def history(device_id, hours):
        captured["hours"] = hours
        return pd.DataFrame({"temperature": [20.0]}, index=[pd.Timestamp("2026-01-01")])

    tools = make_tools(history=history)
    out = tools.get_stats("esp32-aqua-01", "temperature", hours_in)
    assert out["hours_used"] == expected
    assert captured["hours"] == expected


@pytest.mark.parametrize("limit_in, expected", [(0, 1), (-1, 1), (999, 50), (5, 5)])
def test_anomalies_limit_clamped_into_1_to_50(limit_in, expected):
    captured = {}

    def anomalies(device_id, limit):
        captured["limit"] = limit
        return pd.DataFrame()

    tools = make_tools(anomalies=anomalies)
    out = tools.get_anomalies(limit=limit_in)
    assert out["limit_used"] == expected
    assert captured["limit"] == expected


# ---------------------------------------------------------------- empty data

def test_get_latest_empty_device_reports_zero_rows_not_error():
    tools = make_tools(latest=lambda: {})
    out = tools.get_latest(device_id="esp32-aqua-01")
    assert out == {"rows": 0, "device_id": "esp32-aqua-01"}


def test_get_stats_empty_history_reports_zero_rows():
    tools = make_tools(history=lambda d, h: pd.DataFrame())
    out = tools.get_stats("esp32-aqua-01", "temperature", 24)
    assert out["rows"] == 0


def test_anomalies_limit_falls_back_to_default_on_bad_type():
    captured = {}

    def anomalies(device_id, limit):
        captured["limit"] = limit
        return pd.DataFrame()

    tools = make_tools(anomalies=anomalies)
    out = tools.get_anomalies(limit="not-a-number")
    assert out["limit_used"] == 10
    assert captured["limit"] == 10


def test_get_anomalies_empty_reports_zero_rows():
    tools = make_tools(anomalies=lambda d, limit: pd.DataFrame())
    out = tools.get_anomalies()
    assert out == {"rows": 0, "device_id": "all", "limit_used": 10}


def test_get_forecast_empty_reports_zero_rows():
    tools = make_tools(forecasts=lambda d: pd.DataFrame())
    out = tools.get_forecast("esp32-aqua-01")
    assert out == {"rows": 0, "device_id": "esp32-aqua-01"}


# ---------------------------------------------------------------- get_stats: <=48h (raw)

def test_get_stats_raw_window_uses_true_min_max():
    idx = pd.date_range("2026-01-01 00:00", periods=4, freq="10min")
    df = pd.DataFrame({"temperature": [20.0, 29.3, 18.5, 22.0]}, index=idx)
    tools = make_tools(history=lambda d, h: df)

    out = tools.get_stats("esp32-aqua-01", "temperature", 24)

    assert out["rows"] == 4
    assert out["avg"] == round((20.0 + 29.3 + 18.5 + 22.0) / 4, 1)
    assert out["max"]["value"] == 29.3
    assert out["max"]["time"] == "2026-01-01 00:10 (Asia/Taipei)"
    assert out["min"]["value"] == 18.5
    assert out["min"]["time"] == "2026-01-01 00:20 (Asia/Taipei)"


# ---------------------------------------------------------------- get_stats: >48h (hourly rollup)

def test_get_stats_hourly_window_uses_max_min_columns_not_avg_extremes():
    """The known pitfall: for hours>48, max/min MUST come from the
    _max/_min columns' own extremes, not from the _avg column's max/min —
    an hour whose AVERAGE is unremarkable can still contain the actual
    peak reading."""
    idx = pd.date_range("2026-01-01 00:00", periods=3, freq="h")
    df = pd.DataFrame(
        {
            "temperature_avg": [20.0, 20.5, 20.2],  # flat averages
            "temperature_max": [21.0, 31.5, 21.2],  # real spike hidden in hour 2
            "temperature_min": [19.0, 18.0, 19.5],
        },
        index=idx,
    )
    tools = make_tools(history=lambda d, h: df)

    out = tools.get_stats("esp32-aqua-01", "temperature", 72)

    assert out["hours_used"] == 72
    assert out["max"]["value"] == 31.5
    assert out["max"]["time"] == "2026-01-01 01:00 (Asia/Taipei)"
    assert out["min"]["value"] == 18.0
    assert out["min"]["time"] == "2026-01-01 01:00 (Asia/Taipei)"
    # avg is the mean of the hourly averages, not of the per-reading data
    # (that data no longer exists at this resolution)
    assert out["avg"] == round((20.0 + 20.5 + 20.2) / 3, 1)


def test_get_stats_hours_just_over_48_switches_to_hourly_columns():
    raw_df = pd.DataFrame({"temperature": [1.0]}, index=[pd.Timestamp("2026-01-01")])
    hourly_df = pd.DataFrame(
        {"temperature_avg": [20.0], "temperature_max": [21.0], "temperature_min": [19.0]},
        index=[pd.Timestamp("2026-01-01")],
    )

    def history(device_id, hours):
        return raw_df if hours <= 48 else hourly_df

    tools = make_tools(history=history)
    assert tools.get_stats("esp32-aqua-01", "temperature", 48)["avg"] == 1.0
    assert tools.get_stats("esp32-aqua-01", "temperature", 49)["avg"] == 20.0


# ---------------------------------------------------------------- get_latest

def test_get_latest_formats_utc_source_timestamp_as_taipei():
    latest = {
        "esp32-aqua-01": {
            "device_id": "esp32-aqua-01",
            "ts": "2026-01-01T06:00:00+00:00",  # UTC, un-converted (PostgREST raw)
            "temperature": 25.5,
            "humidity": 60.0,
        }
    }
    tools = make_tools(latest=lambda: latest)
    out = tools.get_latest(device_id="esp32-aqua-01")
    assert out["reading"]["time"] == "2026-01-01 14:00 (Asia/Taipei)"  # +8h
    assert out["reading"]["temperature"] == 25.5


def test_get_latest_without_device_id_returns_every_device():
    latest = {
        "esp32-aqua-01": {"device_id": "esp32-aqua-01", "ts": None, "temperature": 1, "humidity": 2},
        "esp32-aqua-02": {"device_id": "esp32-aqua-02", "ts": None, "temperature": 3, "humidity": 4},
    }
    tools = make_tools(latest=lambda: latest)
    out = tools.get_latest()
    assert out["rows"] == 2
    assert {r["device_id"] for r in out["readings"]} == {"esp32-aqua-01", "esp32-aqua-02"}


# ---------------------------------------------------------------- get_anomalies

def test_get_anomalies_caps_note_length_and_tags_taipei_time():
    df = pd.DataFrame(
        {
            "device_id": ["esp32-aqua-01"],
            "ts": [pd.Timestamp("2026-01-01 12:00", tz="Asia/Taipei")],
            "metric": ["temperature"],
            "value": [31.7],
            "method": ["rolling_zscore"],
            "note": ["x" * 500],
        }
    )
    tools = make_tools(anomalies=lambda d, limit: df)
    out = tools.get_anomalies(device_id="esp32-aqua-01")
    row = out["anomalies"][0]
    assert row["time"] == "2026-01-01 12:00 (Asia/Taipei)"
    assert len(row["note"]) == 120


# ---------------------------------------------------------------- get_forecast

def test_get_forecast_keeps_only_latest_per_metric_and_model():
    df = pd.DataFrame(
        [
            {
                "metric": "temperature", "model": "gbm", "horizon_min": 30,
                "ts_target": pd.Timestamp("2026-01-01 13:00", tz="Asia/Taipei"),
                "yhat": 26.0, "yhat_lower": 25.0, "yhat_upper": 27.0,
                "created_at": pd.Timestamp("2026-01-01 12:30", tz="Asia/Taipei"),
            },
            {  # older gbm row for the same metric — should be dropped
                "metric": "temperature", "model": "gbm", "horizon_min": 30,
                "ts_target": pd.Timestamp("2026-01-01 12:30", tz="Asia/Taipei"),
                "yhat": 25.5, "yhat_lower": 24.5, "yhat_upper": 26.5,
                "created_at": pd.Timestamp("2026-01-01 12:00", tz="Asia/Taipei"),
            },
            {
                "metric": "temperature", "model": "ewma+drift", "horizon_min": 30,
                "ts_target": pd.Timestamp("2026-01-01 13:00", tz="Asia/Taipei"),
                "yhat": 26.5, "yhat_lower": None, "yhat_upper": None,
                "created_at": pd.Timestamp("2026-01-01 12:30", tz="Asia/Taipei"),
            },
        ]
    )
    tools = make_tools(forecasts=lambda d: df)
    out = tools.get_forecast("esp32-aqua-01")
    assert out["rows"] == 2
    gbm_row = next(r for r in out["forecasts"] if r["model"] == "gbm")
    assert gbm_row["predicted_value"] == 26.0
    assert gbm_row["range"] == [25.0, 27.0]
    ewma_row = next(r for r in out["forecasts"] if r["model"] == "ewma+drift")
    assert ewma_row["range"] == [None, None]
