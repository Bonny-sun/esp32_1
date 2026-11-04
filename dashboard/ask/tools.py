"""Read-only data tools the LLM can call from the /ask page.

No SQL ever reaches the model — it can only invoke the fixed set of
functions below, each with a validated argument shape. `AskTools` takes its
data access
as constructor-injected loader callables (the same `load_devices`,
`load_history`, `load_anomalies`, `load_forecasts`, `load_all_latest` already
defined in app.py) rather than importing app.py or talking to Supabase
itself — keeps this module importable (and unit-testable with fake loaders)
without pulling in NiceGUI or a real database connection.

Every method returns a plain, JSON-serializable dict — never raises for a
bad argument (invalid device/metric, out-of-range hours/limit all come back
as {"error": "..."} instead), so a single bad tool call can't 500 the page or
break the agent loop. An underlying loader failure (e.g. a dropped Supabase
connection) is left to propagate — agent.py's tool-execution wrapper is
responsible for catching that and reporting it back to the model instead of
tools.py swallowing it here, since tools.py has no opinion on what the agent
loop should do with an unexpected failure.

Output values are deliberately thin: a device_id, a metric name, a number, a
timestamp — never a raw DB row or long free-text blob — so the model has
exact facts to answer from instead of a wall of text to summarize (and
possibly hallucinate around).

可用工具一覽（AskTools 的方法，AI 只能呼叫這幾個）:
    list_devices    — 列出所有裝置
    get_latest      — 某裝置（或全部）最新一筆讀值
    get_stats       — 某裝置某指標近 N 小時的統計(平均/最大/最小)
    get_daily_stats — 某裝置某指標近 N 天的每日統計
    get_anomalies   — 查異常警戒紀錄
    get_forecast    — 查 AI 預測結果
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd

TAIPEI = "Asia/Taipei"
VALID_METRICS = ("temperature", "humidity")
HOURS_MIN, HOURS_MAX = 1, 720  # 720h = 30 days, matching the dashboard's own 30-day history view
DAYS_MIN, DAYS_MAX = 1, 30  # get_daily_stats' own window, same 30-day ceiling expressed in days
LIMIT_MIN, LIMIT_MAX = 1, 50
DEFAULT_ANOMALIES_LIMIT = 10
# Internal only (never exposed in the tool schema): how many rows to pull
# from the loader when get_anomalies is asked to filter by `hours`, so the
# resulting count is accurate for the whole window rather than silently
# capped at the user-facing `limit`. Anomalies are rare in the steady state
# (at most one per device/metric/hour — see aqua_check_thresholds() in
# app.py) but a live demo has shown bursts of ~2/hour across 2 devices while
# a threshold is actively tripped; 3000 leaves comfortable headroom over
# that rate for the full 720h (30-day) window without a second round trip.
ANOMALY_FETCH_CAP = 3000


def _fmt_taipei(ts: Any) -> str | None:
    """Format any timestamp this file touches as Asia/Taipei wall-clock.

    Three different shapes flow through here depending on which loader
    produced the value: load_all_latest()'s raw `ts` is an un-converted
    UTC-aware string straight from PostgREST; load_anomalies()/
    load_forecasts() already return tz-aware pandas Timestamps converted to
    TZ="Asia/Taipei"; load_history()'s index is tz-NAIVE but already holds
    Asia/Taipei wall-clock (app.py adds the +8h itself — see its own
    docstring). `tz_convert` on an aware value and a no-op "already this tz"
    value are both safe; a naive value is left alone and taken at face
    value, which is correct for load_history's case and would be wrong for
    anything that returns naive UTC — nothing currently does."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if pd.isna(t):
        return None
    if t.tzinfo is not None:
        t = t.tz_convert(TAIPEI)
    return t.strftime("%Y-%m-%d %H:%M") + " (Asia/Taipei)"


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """Coerce to int and clamp into [lo, hi]. Never raises — a bad type
    (None, a string that isn't a number, NaN) falls back to `default` rather
    than erroring the whole tool call, since the spec is "clamp", not
    "validate and reject"."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class AskTools:
    """Bind once per request with the real (or fake, in tests) loaders."""

    def __init__(
        self,
        load_devices: Callable[[], list[dict]],
        load_history: Callable[[str, int], pd.DataFrame],
        load_anomalies: Callable[[str | None, int], pd.DataFrame],
        load_forecasts: Callable[[str], pd.DataFrame],
        load_all_latest: Callable[[], dict],
    ) -> None:
        self._load_devices = load_devices
        self._load_history = load_history
        self._load_anomalies = load_anomalies
        self._load_forecasts = load_forecasts
        self._load_all_latest = load_all_latest

    def _device_ids(self) -> set[str]:
        return {d["device_id"] for d in self._load_devices()}

    def _validate_device(self, device_id: str) -> dict | None:
        if device_id not in self._device_ids():
            return {"error": f"unknown device_id {device_id!r}; call list_devices for valid ids"}
        return None

    # ---------------------------------------------------------------- tools

    def list_devices(self) -> dict:
        devices = self._load_devices()
        return {
            "count": len(devices),
            "devices": [{"device_id": d["device_id"], "name": d.get("name") or d["device_id"]} for d in devices],
        }

    def get_latest(self, device_id: str | None = None) -> dict:
        if device_id is not None:
            err = self._validate_device(device_id)
            if err:
                return err
        latest = self._load_all_latest()
        if device_id is not None:
            row = latest.get(device_id)
            if row is None:
                return {"rows": 0, "device_id": device_id}
            return {"rows": 1, "device_id": device_id, "reading": self._format_latest(row)}
        if not latest:
            return {"rows": 0}
        return {"rows": len(latest), "readings": [self._format_latest(r) for r in latest.values()]}

    @staticmethod
    def _format_latest(row: dict) -> dict:
        return {
            "device_id": row.get("device_id"),
            "time": _fmt_taipei(row.get("ts")),
            "temperature": row.get("temperature"),
            "humidity": row.get("humidity"),
        }

    def get_stats(self, device_id: str, metric: str, hours: int) -> dict:
        err = self._validate_device(device_id)
        if err:
            return err
        if metric not in VALID_METRICS:
            return {"error": f"invalid metric {metric!r}; must be one of {list(VALID_METRICS)}"}
        hours_used = _clamp_int(hours, HOURS_MIN, HOURS_MAX, HOURS_MAX)

        df = self._load_history(device_id, hours_used)
        empty = {"rows": 0, "device_id": device_id, "metric": metric, "hours_used": hours_used}
        if df.empty:
            return empty

        # load_history returns two different shapes depending on the window
        # (app.py: raw <=48h vs. the hourly rollup table above that) — see
        # this module's docstring. Raw gives one column per metric (already
        # 10-min-binned means); the hourly table instead gives
        # "{metric}_avg"/"_min"/"_max" columns, so max/min must come from
        # the _max/_min columns' own extremes, never from the _avg column's
        # max/min (that would answer "highest hourly AVERAGE", not "highest
        # single reading").
        if hours_used <= 48:
            if metric not in df.columns:
                return empty
            s = df[metric].dropna()
            if s.empty:
                return empty
            avg, max_s, min_s = s, s, s
        else:
            avg_col, max_col, min_col = f"{metric}_avg", f"{metric}_max", f"{metric}_min"
            if avg_col not in df.columns or max_col not in df.columns or min_col not in df.columns:
                return empty
            avg = df[avg_col].dropna()
            max_s = df[max_col].dropna()
            min_s = df[min_col].dropna()
            if avg.empty or max_s.empty or min_s.empty:
                return empty

        return {
            "rows": int(len(avg)),
            "device_id": device_id,
            "metric": metric,
            "hours_used": hours_used,
            "avg": round(float(avg.mean()), 1),
            "max": {"value": round(float(max_s.max()), 1), "time": _fmt_taipei(max_s.idxmax())},
            "min": {"value": round(float(min_s.min()), 1), "time": _fmt_taipei(min_s.idxmin())},
        }

    def get_daily_stats(self, device_id: str, metric: str, days: int) -> dict:
        """Same avg/max/min shape as get_stats, but one entry per calendar
        day instead of one aggregate for the whole window — for "each day
        this week" questions get_stats structurally cannot answer (it only
        ever returns a single summary across its whole `hours` window)."""
        err = self._validate_device(device_id)
        if err:
            return err
        if metric not in VALID_METRICS:
            return {"error": f"invalid metric {metric!r}; must be one of {list(VALID_METRICS)}"}
        days_used = _clamp_int(days, DAYS_MIN, DAYS_MAX, DAYS_MAX)
        hours_used = days_used * 24

        df = self._load_history(device_id, hours_used)
        empty = {"rows": 0, "device_id": device_id, "metric": metric, "days_used": days_used}
        if df.empty:
            return empty

        # Same raw-vs-hourly distinction as get_stats (see its own comment),
        # just grouped by calendar day afterwards. The index is already
        # Asia/Taipei wall-clock (both branches — see this module's
        # docstring), so grouping by .date directly gives Taipei calendar
        # days with no further tz handling needed.
        daily: list[dict] = []
        if hours_used <= 48:
            if metric not in df.columns:
                return empty
            s = df[metric].dropna()
            if s.empty:
                return empty
            for date, group in s.groupby(s.index.date):
                daily.append(
                    {
                        "date": date.isoformat(),
                        "avg": round(float(group.mean()), 1),
                        "max": {"value": round(float(group.max()), 1), "time": _fmt_taipei(group.idxmax())},
                        "min": {"value": round(float(group.min()), 1), "time": _fmt_taipei(group.idxmin())},
                    }
                )
        else:
            avg_col, max_col, min_col = f"{metric}_avg", f"{metric}_max", f"{metric}_min"
            needed = [avg_col, max_col, min_col]
            if not all(c in df.columns for c in needed):
                return empty
            # dropna() on all three together (not per-column, as get_stats
            # does) keeps each day's avg/max/min rows aligned to the same
            # set of hours — the hourly rollup writes all three from one
            # pass per bucket, so they're never null independently in
            # practice, but grouping a single pre-aligned frame is simpler
            # than reconciling three separately-grouped Series either way.
            sub = df[needed].dropna()
            if sub.empty:
                return empty
            for date, group in sub.groupby(sub.index.date):
                daily.append(
                    {
                        "date": date.isoformat(),
                        "avg": round(float(group[avg_col].mean()), 1),
                        "max": {
                            "value": round(float(group[max_col].max()), 1),
                            "time": _fmt_taipei(group[max_col].idxmax()),
                        },
                        "min": {
                            "value": round(float(group[min_col].min()), 1),
                            "time": _fmt_taipei(group[min_col].idxmin()),
                        },
                    }
                )

        if not daily:
            return empty
        return {
            "rows": len(daily),
            "device_id": device_id,
            "metric": metric,
            "days_used": days_used,
            "daily": daily,  # chronological, oldest first — groupby sorts by key
        }

    def get_anomalies(
        self,
        device_id: str | None = None,
        hours: int | None = None,
        limit: int = DEFAULT_ANOMALIES_LIMIT,
    ) -> dict:
        if device_id is not None:
            err = self._validate_device(device_id)
            if err:
                return err
        limit_used = _clamp_int(limit, LIMIT_MIN, LIMIT_MAX, DEFAULT_ANOMALIES_LIMIT)
        hours_used = _clamp_int(hours, HOURS_MIN, HOURS_MAX, HOURS_MAX) if hours is not None else None

        empty = {"rows": 0, "device_id": device_id or "all", "hours_used": hours_used, "limit_used": limit_used}
        # Without an hours filter the old "most recent N" behavior is enough
        # — fetch exactly limit_used and show all of it. With one, `rows`
        # below must reflect the TRUE count within the window, which the
        # user-facing limit_used must not silently cap — so fetch a much
        # larger batch (ANOMALY_FETCH_CAP) to filter from, and separately
        # cap just the detailed "anomalies" list to limit_used for display.
        fetch_limit = ANOMALY_FETCH_CAP if hours_used is not None else limit_used
        df = self._load_anomalies(device_id, fetch_limit)
        if df.empty:
            return empty

        if hours_used is not None:
            cutoff = pd.Timestamp.now(tz=TAIPEI) - pd.Timedelta(hours=hours_used)
            df = df[df["ts"] >= cutoff]
            if df.empty:
                return empty

        total_rows = len(df)
        rows = []
        for _, r in df.iloc[:limit_used].iterrows():
            note = r.get("note")
            rows.append(
                {
                    "device_id": r.get("device_id"),
                    "time": _fmt_taipei(r.get("ts")),
                    "metric": r.get("metric"),
                    "value": r.get("value"),
                    "type": r.get("method"),
                    # Capped, not omitted: detect_univariate()/aqua_check_thresholds()
                    # write one short factual sentence here (e.g. "temperature
                    # 31.7 deviated +0.6 from 1h avg 31.1"), genuinely useful
                    # for answering "why was this flagged" — but still capped
                    # so a future change to that note format can't let an
                    # unbounded string back into the model.
                    "note": (str(note)[:120] if note else None),
                }
            )
        return {
            "rows": total_rows,  # true count in the window; may exceed len(anomalies) below
            "device_id": device_id or "all",
            "hours_used": hours_used,
            "limit_used": limit_used,
            "anomalies": rows,
        }

    def get_forecast(self, device_id: str) -> dict:
        err = self._validate_device(device_id)
        if err:
            return err
        df = self._load_forecasts(device_id)
        if df.empty:
            return {"rows": 0, "device_id": device_id}
        # One row per (metric, model): load_forecasts already returns newest
        # first, so the first occurrence of each pair is its latest forecast.
        latest = df.drop_duplicates(subset=["metric", "model"], keep="first")
        rows = [
            {
                "metric": r.get("metric"),
                "model": r.get("model"),
                "horizon_min": r.get("horizon_min"),
                "predicted_time": _fmt_taipei(r.get("ts_target")),
                "predicted_value": round(float(r["yhat"]), 1) if pd.notna(r.get("yhat")) else None,
                "range": [
                    round(float(r["yhat_lower"]), 1) if pd.notna(r.get("yhat_lower")) else None,
                    round(float(r["yhat_upper"]), 1) if pd.notna(r.get("yhat_upper")) else None,
                ],
                "created_at": _fmt_taipei(r.get("created_at")),
            }
            for _, r in latest.iterrows()
        ]
        return {"rows": len(rows), "device_id": device_id, "forecasts": rows}


# ---------------------------------------------------------------- schemas
# Plain JSON Schema (OpenAPI-3.0-shaped), handed to the Gemini SDK via
# FunctionDeclaration(parameters_json_schema=...) in agent.py. Kept here
# (not in agent.py) so a tool's declared shape sits next to the method it
# describes — any new tool gets one method above and one entry below.
_DEVICE_ID_OPTIONAL = {"type": "string", "description": "A device_id from list_devices, or omit for every device."}
_DEVICE_ID_REQUIRED = {"type": "string", "description": "A device_id from list_devices."}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_devices",
        "description": "List every registered device's id and name. Call this first if the device isn't known yet.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_latest",
        "description": "Get the most recent temperature/humidity reading for one device, or every device if omitted.",
        "parameters": {
            "type": "object",
            "properties": {"device_id": _DEVICE_ID_OPTIONAL},
        },
    },
    {
        "name": "get_stats",
        "description": (
            "Get ONE average/max/min summary (with when the max/min happened) for temperature or "
            "humidity, aggregated across the entire last N hours for one device. For a question "
            "asking about each day separately (e.g. 'each day this week'), use get_daily_stats "
            "instead — this tool cannot break its result down by day."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": _DEVICE_ID_REQUIRED,
                "metric": {"type": "string", "enum": list(VALID_METRICS)},
                "hours": {
                    "type": "integer",
                    "description": f"Lookback window in hours, {HOURS_MIN}-{HOURS_MAX} (clamped if out of range).",
                },
            },
            "required": ["device_id", "metric", "hours"],
        },
    },
    {
        "name": "get_daily_stats",
        "description": (
            "Get average/max/min (with when the max/min happened) for temperature or humidity, "
            "ONE entry per calendar day, for the last N days for one device. Use this for any "
            "question about each day separately (e.g. 'each day's high/low this week') instead "
            "of get_stats, which only returns one summary for the whole period."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": _DEVICE_ID_REQUIRED,
                "metric": {"type": "string", "enum": list(VALID_METRICS)},
                "days": {
                    "type": "integer",
                    "description": f"Number of most recent days, {DAYS_MIN}-{DAYS_MAX} (clamped if out of range).",
                },
            },
            "required": ["device_id", "metric", "days"],
        },
    },
    {
        "name": "get_anomalies",
        "description": (
            "List flagged anomalies (threshold breaches or statistical deviations), newest first. "
            "The returned 'rows' is the true count — within the 'hours' window if given, otherwise "
            "just how many were fetched — and can be larger than the 'anomalies' list, which is "
            "capped to 'limit' entries of detail. Use 'rows' for counting/statistics questions; "
            "use 'anomalies' for the per-record detail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": _DEVICE_ID_OPTIONAL,
                "hours": {
                    "type": "integer",
                    "description": (
                        f"Only count/list anomalies from the last N hours, {HOURS_MIN}-{HOURS_MAX} "
                        "(clamped if out of range). Omit for 'most recent N' with no time limit."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max rows of detail to return, {LIMIT_MIN}-{LIMIT_MAX} (clamped if out of range).",
                },
            },
        },
    },
    {
        "name": "get_forecast",
        "description": "Get the latest forecast for one device, for each (metric, model) pair.",
        "parameters": {
            "type": "object",
            "properties": {"device_id": _DEVICE_ID_REQUIRED},
            "required": ["device_id"],
        },
    },
]
