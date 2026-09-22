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

import numpy as np
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
# Set DEVICE_ID to restrict a run to one device (handy for local testing).
# Unset (the default in CI) -> every device in aqua_devices gets processed,
# so adding a 2nd/3rd device needs no code or workflow change.
DEVICE_ID = os.environ.get("DEVICE_ID", "")

# >>> the single knob that grows with the hardware <<<
FEATURE_COLS = ["temperature", "humidity"]
# Phase 2:
# FEATURE_COLS = ["temperature", "humidity", "water_temp", "ph", "soil_moisture"]

RESAMPLE = "5min"
LOOKBACK_H = 72        # ewma+drift / anomaly detection: recent-trend window
GBM_LOOKBACK_H = 24 * 14  # gbm: separate, longer window — the whole point of
                          # its hour-of-day feature is learning a diurnal
                          # pattern, which needs many examples per hour-of-day
                          # to be more than noise. 3 days gives ~3 per hour;
                          # 14 gives ~14. Decoupled from LOOKBACK_H on purpose:
                          # widening THAT would blur ewma+drift's "recent
                          # slope" signal, which is a different job.
ROLL_WIN = 12          # 12 * 5 min = 1 h rolling window
Z_THRESH = 3.5
HORIZON_STEPS = 6      # 6 * 5 min = 30 min ahead
GBM_MIN_TRAIN_ROWS = 30   # below this, skip the GBM forecast rather than fit noise

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
        # _prev variants: rolling stats over the PREVIOUS window only (the
        # point at t is not part of the window used to score it). Used by
        # detect_univariate — scoring a point against a window that includes
        # itself caps |z| at (n-1)/sqrt(n), which a real spike can never
        # clear. forecast() intentionally keeps the inclusive _roll_std
        # above as its "current volatility" estimate; that's a different use.
        shifted = out[c].shift(1)
        out[f"{c}_roll_mean_prev"] = shifted.rolling(ROLL_WIN, min_periods=3).mean()
        out[f"{c}_roll_std_prev"] = shifted.rolling(ROLL_WIN, min_periods=3).std()
        out[f"{c}_lag1"] = out[c].shift(1)
    return out


# ---- 3a. baseline forecast: EWMA + linear drift ------------------
def forecast(device_id: str, out: pd.DataFrame, col: str, steps: int) -> dict:
    series = out[col].dropna()
    ewma = series.ewm(span=ROLL_WIN).mean().iloc[-1]
    drift = series.diff().tail(ROLL_WIN).mean()
    sd = float(out[f"{col}_roll_std"].iloc[-1] or 0.0)
    yhat = float(ewma + drift * steps)
    target = out.index[-1] + pd.Timedelta(RESAMPLE) * steps
    return {
        "device_id": device_id,
        "metric": col,
        "horizon_min": steps * 5,
        "ts_target": target.isoformat(),
        "yhat": round(yhat, 2),
        "yhat_lower": round(yhat - 2 * sd, 2),
        "yhat_upper": round(yhat + 2 * sd, 2),
        "model": "ewma+drift",
    }


def _time_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Hour-of-day as sin/cos so a tree model can learn "23:00 and 00:00
    are neighbours" — a raw hour number looks like a cliff to a tree split.
    Works in whatever tz `index` already is (UTC here); the model only
    needs a consistent label per time-of-day, not a human-readable one."""
    hour_frac = index.hour + index.minute / 60.0
    angle = 2 * np.pi * hour_frac / 24.0
    return pd.DataFrame({"hour_sin": np.sin(angle), "hour_cos": np.cos(angle)}, index=index)


# ---- 3a'. challenger forecast: gradient boosting on lag features -----
def forecast_gbm(device_id: str, out: pd.DataFrame, col: str, steps: int) -> dict | None:
    """Direct multi-step forecast: a small gradient-boosted tree trained on
    lag/rolling features (already in `out`) plus time-of-day, so it can
    pick up on a diurnal cycle that ewma+drift's local linear extrapolation
    structurally can't (it only ever looks at the recent slope). Retrained
    from scratch every run on the same window everything else uses — no
    persisted model file, so the pipeline stays stateless like the rest of
    this script.

    Leakage: each training row's target is out[col] shifted BACK by `steps`
    — i.e. a value `steps` rows in that row's future. For every row except
    the last `steps`, that future value already happened and is a real
    historical observation; only the final row's target is genuinely
    unknown, and that's the one row this function actually predicts.
    Returns None (skip this run/metric) if there isn't enough history to
    fit on, so a quiet device never gets a forecast built on noise.
    """
    from sklearn.ensemble import GradientBoostingRegressor

    feat_cols = [col, f"{col}_roll_mean_prev", f"{col}_roll_std_prev", f"{col}_lag1"]
    feats = pd.concat([out[feat_cols], _time_features(out.index)], axis=1)
    target = out[col].shift(-steps)

    train = feats.iloc[:-steps].join(target.iloc[:-steps].rename("y")).dropna()
    if len(train) < GBM_MIN_TRAIN_ROWS:
        return None

    x_now = feats.iloc[[-1]]
    if x_now.isna().any(axis=1).iloc[0]:
        return None

    def _fit(rows: pd.DataFrame) -> GradientBoostingRegressor:
        m = GradientBoostingRegressor(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42)
        m.fit(rows.drop(columns="y"), rows["y"])
        return m

    # Holdout-based residual std for the confidence band — fitting and
    # scoring on the SAME rows (as the point forecast below has to, there
    # being no other data) would understate the true error. Refit on all
    # of `train` afterwards for the actual point prediction, once the
    # holdout has told us how wrong to expect the model to be.
    n_holdout = max(5, int(len(train) * 0.2))
    fit_rows, holdout_rows = train.iloc[:-n_holdout], train.iloc[-n_holdout:]
    if len(fit_rows) < GBM_MIN_TRAIN_ROWS:
        return None
    probe = _fit(fit_rows)
    resid = holdout_rows["y"] - probe.predict(holdout_rows.drop(columns="y"))
    sd = float(resid.std()) if len(holdout_rows) > 1 and pd.notna(resid.std()) else 0.0

    model = _fit(train)
    yhat = float(model.predict(x_now)[0])
    target_ts = out.index[-1] + pd.Timedelta(RESAMPLE) * steps
    return {
        "device_id": device_id,
        "metric": col,
        "horizon_min": steps * 5,
        "ts_target": target_ts.isoformat(),
        "yhat": round(yhat, 2),
        "yhat_lower": round(yhat - 2 * sd, 2),
        "yhat_upper": round(yhat + 2 * sd, 2),
        "model": "gbm",
    }


# ---- 3b. anomaly detection: univariate now, multivariate later ---
def detect_univariate(device_id: str, out: pd.DataFrame, cols: list[str]) -> list[dict]:
    hits: list[dict] = []
    for c in cols:
        dev = out[c] - out[f"{c}_roll_mean_prev"]
        std = out[f"{c}_roll_std_prev"].clip(lower=Z_STD_FLOOR.get(c, 1e-9))
        z = dev / std
        mask = (z.abs() > Z_THRESH) & (dev.abs() > Z_MIN_ABS_DEV.get(c, 0.0))
        for t in out.index[mask]:
            hits.append(
                {
                    "device_id": device_id,
                    "ts": t.isoformat(),
                    "metric": c,
                    "value": float(out.loc[t, c]),
                    "score": round(float(z.loc[t]), 3),
                    "method": "rolling_zscore",
                }
            )
    return hits


def detect_multivariate(device_id: str, out: pd.DataFrame, cols: list[str]) -> list[dict]:
    from sklearn.ensemble import IsolationForest

    x = out[cols].dropna()
    if len(x) < 50:
        return []
    model = IsolationForest(contamination=0.02, random_state=42)
    pred = model.fit_predict(x)
    score = model.score_samples(x)
    return [
        {
            "device_id": device_id,
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


def dedupe_anomalies(device_id: str, rows: list[dict]) -> list[dict]:
    """detect_univariate/detect_multivariate re-scan the whole LOOKBACK_H
    window every run (a run every 30 min, a 72h window -> up to ~144 runs
    see the same point), so without this the same anomaly gets a fresh
    duplicate row inserted on every single run until it ages out of the
    window. Skip anything already stored for this device/metric/method/ts."""
    if not rows:
        return rows
    ts_values = [r["ts"] for r in rows]
    since, until = min(ts_values), max(ts_values)
    existing = (
        sb.table("aqua_anomalies").select("ts,metric,method")
        .eq("device_id", device_id).gte("ts", since).lte("ts", until).execute().data
    )
    # Compare by parsed instant (pandas Timestamp.value), not raw string —
    # Postgres may echo the timestamp back with different precision/format
    # than the isoformat() string that was inserted.
    seen = {(pd.Timestamp(e["ts"]).value, e["metric"], e["method"]) for e in existing}
    return [
        r for r in rows
        if (pd.Timestamp(r["ts"]).value, r["metric"], r["method"]) not in seen
    ]


def list_device_ids() -> list[str]:
    """DEVICE_ID env var restricts a run to one device (local testing).
    Otherwise every device in aqua_devices is processed, so a 2nd/3rd
    device starts getting forecasts automatically once it's registered."""
    if DEVICE_ID:
        return [DEVICE_ID]
    rows = sb.table("aqua_devices").select("device_id").execute().data
    return [r["device_id"] for r in rows]


def process_device(device_id: str) -> None:
    df = load_history(device_id, LOOKBACK_H)
    if df.empty:
        print(f"[{device_id}] no telemetry yet — collect some data first")
        return

    feats = build_features(df, FEATURE_COLS)
    print(f"[{device_id}] {len(feats)} resampled rows | features = {FEATURE_COLS}")

    forecasts = [forecast(device_id, feats, c, HORIZON_STEPS) for c in FEATURE_COLS]

    # GBM gets its own, longer-history feature frame (see GBM_LOOKBACK_H) —
    # df is non-empty at this point, and GBM_LOOKBACK_H > LOOKBACK_H with the
    # same "now" upper bound, so gbm_df is guaranteed non-empty too.
    gbm_df = load_history(device_id, GBM_LOOKBACK_H)
    gbm_feats = build_features(gbm_df, FEATURE_COLS)
    print(f"[{device_id}] {len(gbm_feats)} resampled rows for gbm training ({GBM_LOOKBACK_H}h lookback)")
    for c in FEATURE_COLS:
        gbm = forecast_gbm(device_id, gbm_feats, c, HORIZON_STEPS)
        if gbm is not None:
            forecasts.append(gbm)
    for f in forecasts:
        print(
            f"  [{device_id}] forecast {f['model']:10s} {f['metric']:14s} +{f['horizon_min']:>3}min -> "
            f"{f['yhat']:>7}  [{f['yhat_lower']}, {f['yhat_upper']}]"
        )
    save("aqua_forecasts", forecasts)

    if len(FEATURE_COLS) >= 3:
        anomalies = detect_multivariate(device_id, feats, FEATURE_COLS)
    else:
        anomalies = detect_univariate(device_id, feats, FEATURE_COLS)
    anomalies = dedupe_anomalies(device_id, anomalies)
    print(f"  [{device_id}] {len(anomalies)} new anomalies")
    save("aqua_anomalies", anomalies)


# ---- main ----------------------------------------------------------
def main() -> None:
    device_ids = list_device_ids()
    if not device_ids:
        print("no devices registered in aqua_devices")
        return
    for device_id in device_ids:
        process_device(device_id)


if __name__ == "__main__":
    main()
