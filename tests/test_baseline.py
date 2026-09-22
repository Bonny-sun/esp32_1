import baseline
import numpy as np
import pandas as pd
import pytest


def _frame(values, cols=("temperature",), start="2026-09-16 00:00", freq="5min"):
    idx = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame({c: values for c in cols}, index=idx)


# ---- load_history: PostgREST 1000-row cap ------------------------------
def test_load_history_pages_newest_first_until_short_page(monkeypatch, fake_client):
    total = 2005
    ts = pd.date_range("2026-09-16", periods=total, freq="min", tz="UTC")
    newest_first = [{"ts": t.isoformat(), "temperature": 25.0, "humidity": 50.0} for t in ts[::-1]]

    def handler(q):
        (lo, hi), _ = q.op("range")
        return newest_first[lo:hi + 1]

    client = fake_client(handler)
    monkeypatch.setattr(baseline, "sb", client)

    df = baseline.load_history("esp32-aqua-01", 72)

    assert len(client.queries) == 3               # 1000 + 1000 + 5, stops on short page
    assert len(df) == total                       # nothing hidden by the cap
    assert df.index.is_monotonic_increasing       # re-sorted oldest -> newest
    assert str(df.index.tz) == "UTC"
    assert client.queries[0].op("order")[1] == {"desc": True}


def test_load_history_empty_returns_empty_frame(monkeypatch, fake_client):
    monkeypatch.setattr(baseline, "sb", fake_client(lambda q: []))
    assert baseline.load_history("x", 1).empty


def test_load_history_handles_mixed_iso_precision(monkeypatch, fake_client):
    rows = [
        {"ts": "2026-09-16T00:00:01.123456+00:00", "temperature": 1, "humidity": 2},
        {"ts": "2026-09-16T00:00:00+00:00", "temperature": 1, "humidity": 2},
    ]
    monkeypatch.setattr(baseline, "sb", fake_client(lambda q: rows))
    assert len(baseline.load_history("x", 1)) == 2  # format="ISO8601" copes with both


# ---- build_features ----------------------------------------------------
def test_build_features_adds_rolling_and_lag_columns():
    out = baseline.build_features(_frame(range(30)), ["temperature"])
    assert {
        "temperature", "temperature_roll_mean", "temperature_roll_std",
        "temperature_roll_mean_prev", "temperature_roll_std_prev", "temperature_lag1",
    } <= set(out.columns)
    assert out["temperature_lag1"].iloc[5] == out["temperature"].iloc[4]


def test_build_features_interpolates_short_gaps_only():
    vals = [20.0] * 5 + [np.nan] * 2 + [22.0] * 5
    out = baseline.build_features(_frame(vals), ["temperature"])
    assert out["temperature"].notna().all()       # 2-step gap <= limit=3


# ---- forecast ----------------------------------------------------------
def test_forecast_extrapolates_a_linear_ramp():
    out = baseline.build_features(_frame([20 + 0.1 * i for i in range(60)]), ["temperature"])
    f = baseline.forecast("dev", out, "temperature", baseline.HORIZON_STEPS)

    last = out["temperature"].iloc[-1]
    assert f["horizon_min"] == 30 and f["model"] == "ewma+drift"
    assert f["yhat"] > last                       # rising trend projects upward
    assert f["yhat_lower"] <= f["yhat"] <= f["yhat_upper"]
    assert pd.Timestamp(f["ts_target"]) == out.index[-1] + pd.Timedelta("30min")


def test_forecast_flat_series_stays_flat():
    out = baseline.build_features(_frame([25.0] * 40), ["temperature"])
    f = baseline.forecast("dev", out, "temperature", baseline.HORIZON_STEPS)
    assert f["yhat"] == 25.0


# ---- forecast_gbm --------------------------------------------------------
def _diurnal_frame(days=3, freq="5min", amplitude=5.0, base=25.0, peak_hour=18.0):
    idx = pd.date_range("2026-09-16", periods=int(days * 24 * 60 / 5), freq=freq, tz="UTC")
    hours = idx.hour + idx.minute / 60.0
    vals = base + amplitude * np.sin(2 * np.pi * (hours - (peak_hour - 6)) / 24)
    return pd.DataFrame({"temperature": vals}, index=idx)


def test_forecast_gbm_returns_none_with_too_little_data():
    pytest.importorskip("sklearn")
    out = baseline.build_features(_frame(range(10)), ["temperature"])
    assert baseline.forecast_gbm("dev", out, "temperature", baseline.HORIZON_STEPS) is None


def test_forecast_gbm_predicts_a_sane_value_on_a_diurnal_signal():
    pytest.importorskip("sklearn")
    out = baseline.build_features(_diurnal_frame(days=3), ["temperature"])
    f = baseline.forecast_gbm("dev", out, "temperature", baseline.HORIZON_STEPS)

    assert f is not None
    assert f["model"] == "gbm"
    assert f["device_id"] == "dev" and f["metric"] == "temperature"
    assert f["horizon_min"] == 30
    assert 15.0 <= f["yhat"] <= 35.0               # within the signal's own range, no blow-up
    assert f["yhat_lower"] <= f["yhat"] <= f["yhat_upper"]
    assert pd.Timestamp(f["ts_target"]) == out.index[-1] + pd.Timedelta("30min")


def test_forecast_gbm_is_deterministic():
    """Same input twice -> same output. Guards against an unseeded model
    or any hidden dependence on call order/global state creeping in."""
    pytest.importorskip("sklearn")
    out = baseline.build_features(_diurnal_frame(days=3), ["temperature"])
    f1 = baseline.forecast_gbm("dev", out, "temperature", baseline.HORIZON_STEPS)
    f2 = baseline.forecast_gbm("dev", out, "temperature", baseline.HORIZON_STEPS)
    assert f1 == f2


def test_forecast_gbm_training_targets_never_reach_into_the_unknown_future():
    """The row being predicted (out.index[-1]) has no real target yet — its
    y would be out[col] at a row that doesn't exist. Confirms the training
    frame excludes exactly the trailing `steps` rows as feature rows (their
    target is NaN and gets dropped), so the model is never fit on a made-up
    label for "now"."""
    pytest.importorskip("sklearn")
    out = baseline.build_features(_diurnal_frame(days=3), ["temperature"])
    steps = baseline.HORIZON_STEPS

    feat_cols = ["temperature", "temperature_roll_mean_prev", "temperature_roll_std_prev", "temperature_lag1"]
    feats = pd.concat([out[feat_cols], baseline._time_features(out.index)], axis=1)
    target = out["temperature"].shift(-steps)
    train = feats.iloc[:-steps].join(target.iloc[:-steps].rename("y")).dropna()

    # the "now" row must never appear as a training example
    assert out.index[-1] not in train.index


# ---- z-score gating ----------------------------------------------------
def test_zscore_flags_a_real_spike():
    vals = [25.0 + 0.1 * (i % 2) for i in range(40)]
    vals[30] = 32.0
    out = baseline.build_features(_frame(vals), ["temperature"])

    hits = baseline.detect_univariate("dev", out, ["temperature"])

    assert [h["method"] for h in hits] == ["rolling_zscore"] * len(hits)
    assert any(pd.Timestamp(h["ts"]) == out.index[30] for h in hits)
    assert all(h["metric"] == "temperature" and h["device_id"] == "dev" for h in hits)


def test_zscore_ignores_tiny_wiggle_on_flat_signal():
    """The regression the gates exist for: a near-flat signal collapses the
    rolling std, so a 0.3 °C blip used to score as many sigma."""
    vals = [25.0] * 40
    vals[30] = 25.3
    out = baseline.build_features(_frame(vals), ["temperature"])
    assert baseline.detect_univariate("dev", out, ["temperature"]) == []


# ---- multivariate ------------------------------------------------------
def test_multivariate_needs_enough_rows():
    pytest.importorskip("sklearn")
    cols = ["temperature", "humidity", "ph"]
    out = _frame(np.random.default_rng(0).normal(size=30), cols)
    assert baseline.detect_multivariate("dev", out, cols) == []


def test_multivariate_flags_an_outlier():
    pytest.importorskip("sklearn")
    cols = ["temperature", "humidity", "ph"]
    rng = np.random.default_rng(0)
    out = pd.DataFrame(rng.normal(0, 0.1, (200, 3)), columns=cols,
                       index=pd.date_range("2026-09-16", periods=200, freq="5min", tz="UTC"))
    out.iloc[100] = [50, 50, 50]

    hits = baseline.detect_multivariate("dev", out, cols)

    assert any(pd.Timestamp(h["ts"]) == out.index[100] for h in hits)
    assert {h["method"] for h in hits} == {"isolation_forest"}


# ---- dedupe_anomalies --------------------------------------------------
def _anomaly(ts, metric="temperature", method="rolling_zscore"):
    return {"device_id": "dev", "ts": ts, "metric": metric, "value": 1.0,
            "score": 4.0, "method": method}


def test_dedupe_drops_rows_already_stored_despite_timestamp_format(monkeypatch, fake_client):
    stored = [{"ts": "2026-09-16T00:00:00+00:00", "metric": "temperature", "method": "rolling_zscore"}]
    monkeypatch.setattr(baseline, "sb", fake_client(lambda q: stored))
    rows = [
        _anomaly("2026-09-16T00:00:00+00:00"),                       # duplicate
        _anomaly("2026-09-16T00:00:00+00:00", metric="humidity"),    # other metric: keep
        _anomaly("2026-09-16T00:05:00+00:00"),                       # new instant: keep
    ]

    kept = baseline.dedupe_anomalies("dev", rows)

    assert [(r["ts"], r["metric"]) for r in kept] == [
        ("2026-09-16T00:00:00+00:00", "humidity"),
        ("2026-09-16T00:05:00+00:00", "temperature"),
    ]


def test_dedupe_empty_input_makes_no_query(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(baseline, "sb", client)
    assert baseline.dedupe_anomalies("dev", []) == []
    assert client.queries == []


# ---- device selection --------------------------------------------------
def test_list_device_ids_reads_registry(monkeypatch, fake_client):
    monkeypatch.setattr(baseline, "DEVICE_ID", "")
    monkeypatch.setattr(baseline, "sb", fake_client(lambda q: [{"device_id": "a"}, {"device_id": "b"}]))
    assert baseline.list_device_ids() == ["a", "b"]


def test_list_device_ids_env_override_skips_db(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(baseline, "DEVICE_ID", "only-one")
    monkeypatch.setattr(baseline, "sb", client)
    assert baseline.list_device_ids() == ["only-one"]
    assert client.queries == []


# ---- process_device: champion-challenger integration --------------------
def test_process_device_saves_both_forecast_models(monkeypatch, fake_client):
    pytest.importorskip("sklearn")
    idx = pd.date_range("2026-09-16", periods=3 * 24 * 12, freq="5min", tz="UTC")
    hours = idx.hour + idx.minute / 60.0
    temps = 25 + 5 * np.sin(2 * np.pi * (hours - 12) / 24)
    hums = 60 + 3 * np.sin(2 * np.pi * (hours - 18) / 24)
    telemetry_newest_first = [
        {"ts": t.isoformat(), "temperature": float(tv), "humidity": float(hv)}
        for t, tv, hv in zip(idx, temps, hums, strict=True)
    ][::-1]

    saved: dict[str, list] = {"aqua_forecasts": [], "aqua_anomalies": []}

    def handler(q):
        if q.table == "aqua_telemetry":
            (lo, hi), _ = q.op("range")
            return telemetry_newest_first[lo:hi + 1]
        if q.table in saved:
            if q.has("insert"):
                rows = q.op("insert")[0][0]
                saved[q.table].extend(rows)
                return rows
            return []                       # dedupe_anomalies' pre-existing-rows lookup
        return []

    monkeypatch.setattr(baseline, "sb", fake_client(handler))

    baseline.process_device("dev")

    models_by_metric: dict[str, set] = {}
    for r in saved["aqua_forecasts"]:
        models_by_metric.setdefault(r["metric"], set()).add(r["model"])
    assert models_by_metric == {
        "temperature": {"ewma+drift", "gbm"},
        "humidity": {"ewma+drift", "gbm"},
    }


def test_process_device_fetches_a_longer_window_for_gbm(monkeypatch, fake_client):
    """GBM_LOOKBACK_H is supposed to be its own, longer history fetch — not
    just the LOOKBACK_H window reused. Locks in that process_device asks
    load_history for both windows, with GBM's strictly longer, rather than
    silently training on the same short window as ewma+drift."""
    pytest.importorskip("sklearn")
    idx = pd.date_range("2026-09-16", periods=3 * 24 * 12, freq="5min", tz="UTC")
    telemetry_newest_first = [
        {"ts": t.isoformat(), "temperature": 25.0, "humidity": 60.0} for t in idx
    ][::-1]

    def handler(q):
        if q.table == "aqua_telemetry":
            (lo, hi), _ = q.op("range")
            return telemetry_newest_first[lo:hi + 1]
        return []

    monkeypatch.setattr(baseline, "sb", fake_client(handler))

    calls: list[int] = []
    real_load_history = baseline.load_history

    def spy(device_id, hours):
        calls.append(hours)
        return real_load_history(device_id, hours)

    monkeypatch.setattr(baseline, "load_history", spy)
    baseline.process_device("dev")

    assert calls == [baseline.LOOKBACK_H, baseline.GBM_LOOKBACK_H]
    assert baseline.GBM_LOOKBACK_H > baseline.LOOKBACK_H
