#!/usr/bin/env python3
"""
Phase-1 baseline analysis
=========================
Pull temperature/humidity history from Supabase -> resample -> short-horizon
forecast + anomaly flags -> write results back to aqua_forecasts / aqua_anomalies.

Extensibility
-------------
`FEATURE_COLS` is the ONLY line that changes when Phase-2 sensors come online.
Once 3+ features are present the anomaly detector automatically switches from
a univariate rolling z-score to a multivariate IsolationForest — same inputs,
same output table, no pipeline change.

Run locally:
    cd analysis
    cp .env.example .env      # then edit
    pip install -r requirements.txt
    python baseline.py

Run in the cloud:
    a Render Cron Job or GitHub Action that executes `python baseline.py`
    on a schedule (e.g. every 15 min).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np  # noqa: F401  (kept: handy in the console / Phase-2 maths)
import pandas as pd
from supabase import create_client

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DEVICE_ID = os.environ.get("DEVICE_ID", "esp32-aqua-01")

# >>> the single knob that grows with the hardware <<<
FEATURE_COLS = ["temperature", "humidity"]
# Phase 2:
# FEATURE_COLS = ["temperature", "humidity", "water_temp", "ph", "soil_moisture"]

RESAMPLE = "5min"
LOOKBACK_H = 72
ROLL_WIN = 12          # 12 * 5 min = 1 h rolling window
Z_THRESH = 3.5
HORIZON_STEPS = 6      # 6 * 5 min = 30 min ahead

# Anti-false-positive gates for the z-score detector. On a near-flat signal
# the rolling std collapses, so a trivial wiggle scores many sigma. Require
# BOTH a high z AND a meaningful absolute move; also floor the std.
Z_MIN_ABS_DEV = {"temperature": 0.8, "humidity": 3.0,
                 "water_temp": 0.5, "ph": 0.15, "soil_moisture": 5.0}
Z_STD_FLOOR = {"temperature": 0.15, "humidity": 0.5,
               "water_temp": 0.1, "ph": 0.02, "soil_moisture": 1.0}

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---- 1. ingest ---------------------------------------------------------
def load_history(device_id: str, hours: int) -> pd.DataFrame:
    """Page through the window NEWEST-first. PostgREST caps a response near
    1000 rows, so a plain ascending .limit() would only return the oldest
    ~1000 rows (~16 h) of a multi-day lookback."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cols = ",".join(["ts", *FEATURE_COLS])
    rows: list = []
    for page in range(60):  # 60 * 1000 = 60k row ceiling
        chunk = (
            sb.table("aqua_telemetry")
            .select(cols)
            .eq("device_id", device_id)
            .gte("ts", since)
            .order("ts", desc=True)
            .range(page * 1000, page * 1000 + 999)
            .execute()
            .data
        )
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df.set_index("ts").sort_index()


# ---- 2. feature frame (extensible) ---------------------------------
def build_features(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    num = df[cols].apply(pd.to_numeric, errors="coerce")
    out = num.resample(RESAMPLE).mean().interpolate(limit=3)
    for c in cols:
        out[f"{c}_roll_mean"] = out[c].rolling(ROLL_WIN, min_periods=3).mean()
        out[f"{c}_roll_std"] = out[c].rolling(ROLL_WIN, min_periods=3).std()
        out[f"{c}_lag1"] = out[c].shift(1)
    return out


# ---- 3a. baseline forecast: EWMA + linear drift ------------------
def forecast(out: pd.DataFrame, col: str, steps: int) -> dict:
    series = out[col].dropna()
    ewma = series.ewm(span=ROLL_WIN).mean().iloc[-1]
    drift = series.diff().tail(ROLL_WIN).mean()
    sd = float(out[f"{col}_roll_std"].iloc[-1] or 0.0)
    yhat = float(ewma + drift * steps)
    target = out.index[-1] + pd.Timedelta(RESAMPLE) * steps
    return {
        "device_id": DEVICE_ID,
        "metric": col,
        "horizon_min": steps * 5,
        "ts_target": target.isoformat(),
        "yhat": round(yhat, 2),
        "yhat_lower": round(yhat - 2 * sd, 2),
        "yhat_upper": round(yhat + 2 * sd, 2),
        "model": "ewma+drift",
    }


# ---- 3b. anomaly detection: univariate now, multivariate later ---
def detect_univariate(out: pd.DataFrame, cols: list[str]) -> list[dict]:
    hits: list[dict] = []
    for c in cols:
        dev = out[c] - out[f"{c}_roll_mean"]
        std = out[f"{c}_roll_std"].clip(lower=Z_STD_FLOOR.get(c, 1e-9))
        z = dev / std
        mask = (z.abs() > Z_THRESH) & (dev.abs() > Z_MIN_ABS_DEV.get(c, 0.0))
        for t in out.index[mask]:
            hits.append(
                {
                    "device_id": DEVICE_ID,
                    "ts": t.isoformat(),
                    "metric": c,
                    "value": float(out.loc[t, c]),
                    "score": round(float(z.loc[t]), 3),
                    "method": "rolling_zscore",
                }
            )
    return hits


def detect_multivariate(out: pd.DataFrame, cols: list[str]) -> list[dict]:
    from sklearn.ensemble import IsolationForest

    x = out[cols].dropna()
    if len(x) < 50:
        return []
    model = IsolationForest(contamination=0.02, random_state=42)
    pred = model.fit_predict(x)
    score = model.score_samples(x)
    return [
        {
            "device_id": DEVICE_ID,
            "ts": t.isoformat(),
            "metric": "multivariate",
            "value": None,
            "score": round(float(score[i]), 3),
            "method": "isolation_forest",
        }
        for i, t in enumerate(x.index)
        if pred[i] == -1
    ]


# ---- 4. persist ------------------------------------------------------
def save(table: str, rows: list[dict]) -> None:
    if rows:
        sb.table(table).insert(rows).execute()


# ---- main ----------------------------------------------------------
def main() -> None:
    df = load_history(DEVICE_ID, LOOKBACK_H)
    if df.empty:
        print("no telemetry yet — collect some data first")
        return

    feats = build_features(df, FEATURE_COLS)
    print(f"{len(feats)} resampled rows | features = {FEATURE_COLS}")

    forecasts = [forecast(feats, c, HORIZON_STEPS) for c in FEATURE_COLS]
    for f in forecasts:
        print(
            f"  forecast {f['metric']:14s} +{f['horizon_min']:>3}min -> "
            f"{f['yhat']:>7}  [{f['yhat_lower']}, {f['yhat_upper']}]"
        )
    save("aqua_forecasts", forecasts)

    if len(FEATURE_COLS) >= 3:
        anomalies = detect_multivariate(feats, FEATURE_COLS)
    else:
        anomalies = detect_univariate(feats, FEATURE_COLS)
    print(f"  {len(anomalies)} anomalies")
    save("aqua_anomalies", anomalies)


if __name__ == "__main__":
    main()
