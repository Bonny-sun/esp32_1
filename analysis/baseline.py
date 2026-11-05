#!/usr/bin/env python3
"""
╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗

類別：AI 分析（Analysis）── 預測與異常偵測，用 Python（pandas / scikit-learn）寫成。
目的：把資料庫裡的溫濕度歷史資料拿來「看未來、找怪事」，共做 4 件事：
      1. 預測未來 30 分鐘的溫度、濕度（兩種模型：基準 ewma+drift、挑戰 gbm）
      2. 找出異常的數值（突然飆高或掉下去）
      3. 把預測與異常寫回資料庫，儀表板和 LINE 才看得到
      4. 預測時間到了之後，拿真實讀值回頭比對，算出「預測準不準」
資料流向：Supabase 歷史資料 ──> 【本程式】 ──> 預測表 aqua_forecasts / 異常表 aqua_anomalies
          ──> 儀表板 /forecast、/anomalies 頁面
誰來執行：worker/ingest.py 每 30 分鐘呼叫一次 main()（也可以在自己電腦手動執行）

程式架構：
  一、設定與常數        【1-1】載入 .env 與環境變數  【1-2】可調整的參數  【1-3】建立資料庫連線
  二、載入與整理資料    【2-1】從資料庫載入歷史資料  【2-2】整理成「特徵」
  三、預測              【3-1】基準模型 ewma+drift  【3-2】時間特徵  【3-3】挑戰模型 gbm
  四、異常偵測          【4-1】單變量 z-score  【4-2】多變量 IsolationForest
  五、寫回資料庫        【5-1】寫入  【5-2】去除重複的異常
  六、結算舊預測        【6-1】預測時間到了，用真實讀值算命中與誤差
  七、主流程            【7-1】列出要處理的裝置  【7-2】單一裝置完整流程  【7-3】main 入口

備註：
  新增感測器欄位 ........ 【1-2】的 FEATURE_COLS（其他部分會自動跟著動）
  改預測多遠 ............ 【1-2】的 HORIZON_STEPS
  改異常多敏感 .......... 【1-2】的 Z_THRESH、Z_MIN_ABS_DEV
  改看多久的歷史資料 .... 【1-2】的 LOOKBACK_H、GBM_LOOKBACK_H
  改整體流程順序 ........ 【7-2】
  名詞：特徵(feature) = 整理後拿去給模型算的數字（如移動平均）
        ewma = 近期資料權重比較高的平均    z-score = 偏離平均幾個「標準差」，越大越怪
        gbm = 梯度提升樹，一種會學習規律的機器學習模型    IsolationForest = 專門找離群值的模型

╚════════════════════════════════════════════════════════════════════════════╝

以下為技術備註（原文保留）：

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
    worker/ingest.py (the always-on Render worker) imports this module and
    calls main() on its own internal 30-min timer — no external scheduler.
    The GitHub Actions `forecast` workflow is manual-only (Run workflow
    button), kept for testing against the real database.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from supabase import create_client

# ============================================================================
# 一、設定與常數：要分析哪些欄位、看多久的資料、多敏感，都在這裡調整
# ============================================================================

# ---- 【1-1】載入 .env 與環境變數（資料庫網址與金鑰放在環境變數，不寫死在程式裡）----
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

# ---- 【1-2】可調整的參數 ----
# FEATURE_COLS = 要分析哪些感測欄位；RESAMPLE = 資料整理成每 5 分鐘一筆；
# LOOKBACK_H = 基準預測與異常偵測看「近幾小時」；GBM_LOOKBACK_H = gbm 模型看更長的歷史(14 天)；
# ROLL_WIN = 移動窗口大小(12 筆 = 1 小時)；Z_THRESH = z-score 超過幾才算異常；
# HORIZON_STEPS = 預測往後幾步(6 步 × 5 分鐘 = 30 分鐘)；GBM_MIN_TRAIN_ROWS = 資料太少就不預測
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

# ---- 【1-3】建立資料庫連線 sb（下面所有函式讀寫 Supabase 都用它）----
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================================
# 二、載入與整理資料：先把歷史資料撈出來，再整理成模型好算的格式
# ============================================================================

# ---- 【2-1】從資料庫載入某台裝置近 N 小時的歷史資料（一次只能撈約 1000 筆，所以分頁撈）----
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


# ---- 【2-2】整理成「特徵」：①每 5 分鐘取平均 ②補上短暫的空缺 ③算移動平均、移動標準差、前一筆的值 ----
# 欄位名稱規則：<指標>_roll_mean = 近 1 小時平均；_roll_std = 近 1 小時波動；_lag1 = 前一筆；
# _prev 結尾 = 「不含自己」的版本，異常偵測用（才不會拿自己跟含自己的平均比）
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


# ============================================================================
# 三、預測：兩個模型各自預測 30 分鐘後的數值，再一起寫進資料庫比較誰比較準
# ============================================================================

# ---- 【3-1】基準模型 ewma+drift：預測值 = 近期加權平均 + 近期每步平均變化量 × 往後幾步 ----
# 簡單說：「如果最近一直在升溫，就假設接下來繼續照這個速度升」。信賴範圍 = 預測值 ± 2 倍近期波動
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


# ---- 【3-2】時間特徵：把「幾點」轉成 sin/cos 兩個數字，讓模型知道 23:00 和 00:00 其實很接近 ----
def _time_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Hour-of-day as sin/cos so a tree model can learn "23:00 and 00:00
    are neighbours" — a raw hour number looks like a cliff to a tree split.
    Works in whatever tz `index` already is (UTC here); the model only
    needs a consistent label per time-of-day, not a human-readable one."""
    hour_frac = index.hour + index.minute / 60.0
    angle = 2 * np.pi * hour_frac / 24.0
    return pd.DataFrame({"hour_sin": np.sin(angle), "hour_cos": np.cos(angle)}, index=index)


# ---- 【3-3】挑戰模型 gbm：用過去 14 天資料「學」出規律（如一天中幾點會升溫），再預測 30 分鐘後 ----
# 步驟：①準備特徵(前一筆、近 1 小時平均/波動、幾點) ②每筆的「答案」= 30 分鐘後的真實值
#       ③留 20% 當考題，算出模型平常會錯多少 → 當信賴範圍 ④用全部資料重練一次，做正式預測
# 資料不夠(少於 30 筆)就回傳 None，不硬預測。
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


# ============================================================================
# 四、異常偵測：感測欄位少於 3 個用 4-1，3 個以上自動換成 4-2（由 7-2 決定）
# ============================================================================

# ---- 【4-1】單變量 z-score：某一筆比「前 1 小時平均」多偏離 3.5 個標準差、而且絕對差距夠大，才算異常 ----
# 兩個條件都要成立，是為了避免數值幾乎不動時，一點小波動就被誤判成異常
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
                    # threshold anomalies get an English note from the SQL
                    # side ("temperature 30.9 out of band [22, 30.5]") —
                    # match that style so 近期異常's 說明 column isn't blank
                    # for this method too.
                    "note": (
                        f"{c} {out.loc[t, c]:.1f} deviated {dev.loc[t]:+.1f} "
                        f"from 1h avg {out.loc[t, f'{c}_roll_mean_prev']:.1f} "
                        f"(z={z.loc[t]:.1f})"
                    ),
                }
            )
    return hits


# ---- 【4-2】多變量 IsolationForest：同時看多個欄位，找出「組合起來很少見」的那 2% 資料點 ----
# 目前只有溫度、濕度兩欄，所以還沒啟用；等加了水溫、pH 等感測器（3 欄以上）才會用到
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


# ============================================================================
# 五、寫回資料庫：把預測與異常存起來，儀表板和 LINE 通知才讀得到
# ============================================================================

# ---- 【5-1】寫入：把一批資料(預測或異常)新增到指定的資料表，沒資料就跳過 ----
# ---- 4. persist ------------------------------------------------------
def save(table: str, rows: list[dict]) -> None:
    if rows:
        sb.table(table).insert(rows).execute()


# ---- 【5-2】去除重複的異常：每 30 分鐘都會重掃 72 小時，同一個異常會被反覆發現，已存過的就不再存 ----
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


# ============================================================================
# 六、結算舊預測：預測的目標時間過了之後，回頭拿真實讀值比對，算出準不準
#     （儀表板「預測 vs 實際」直接讀這裡算好的結果，不用每次開頁面再重算）
# ============================================================================

# ---- 【6-1】結算：對每筆「時間已過、還沒比對」的預測，找最接近的真實讀值，記下實際值、誤差、有沒有落在信賴範圍內 ----
# 找不到 15 分鐘內的真實讀值就跳過；超過 72 小時還沒配到的預測就不再重試
# ---- 4b. settle due forecasts (pre-compute 「預測 vs 實際」) -----------
# How far back to retry matching an unsettled forecast. Bounded so a
# forecast that can never be matched (e.g. the device went offline right
# as its ts_target arrived, and never came back) doesn't get re-queried
# forever — it just stays unsettled (actual/err/hit NULL) past this age
# and drops out of the pending set. Matches LOOKBACK_H's own 72h telemetry
# window below, so `telemetry` always has enough history to cover whatever
# this query returns.
SETTLE_MAX_AGE_H = LOOKBACK_H


def settle_due_forecasts(device_id: str, telemetry: pd.DataFrame) -> int:
    """Fill in actual/err/hit for forecasts whose ts_target has already
    passed but haven't been matched against a real reading yet. Matches
    against `telemetry` (the same frame process_device() already loaded
    for forecasting — its 72h window comfortably covers anything due this
    run, since this runs every ~30 min), so no extra Supabase round trip
    is needed just to fetch comparison data.

    This is the computation the /forecast dashboard page used to redo on
    every view (paging through days of raw telemetry to match against);
    doing it once here, in the background job that already runs on a
    schedule, means the dashboard can just read the stored result instead.
    Returns how many forecasts got settled, for the caller's own logging."""
    if telemetry.empty:
        return 0
    now = datetime.now(timezone.utc)
    pending = (
        sb.table("aqua_forecasts")
        .select("id,metric,ts_target,yhat,yhat_lower,yhat_upper")
        .eq("device_id", device_id)
        .is_("actual", "null")
        .lt("ts_target", now.isoformat())
        .gte("ts_target", (now - timedelta(hours=SETTLE_MAX_AGE_H)).isoformat())
        .execute().data
    )
    settled = 0
    for row in pending:
        metric = row["metric"]
        if metric not in telemetry.columns:
            continue
        s = telemetry[metric].dropna()
        if s.empty:
            continue
        target = pd.Timestamp(row["ts_target"])
        pos = s.index.get_indexer([target], method="nearest")[0]
        if abs((s.index[pos] - target).total_seconds()) > 900:  # >15min: no real match
            continue
        actual = float(s.iloc[pos])
        yhat_lower, yhat_upper = row.get("yhat_lower"), row.get("yhat_upper")
        hit = (
            yhat_lower is not None and yhat_upper is not None
            and yhat_lower <= actual <= yhat_upper
        )
        sb.table("aqua_forecasts").update(
            {"actual": round(actual, 2), "err": round(abs(actual - row["yhat"]), 2), "hit": hit}
        ).eq("id", row["id"]).execute()
        settled += 1
    return settled


# ============================================================================
# 七、主流程：決定要處理哪些裝置，並對每台裝置依序執行上面的步驟
# ============================================================================

# ---- 【7-1】列出要處理的裝置：預設全部裝置；設了環境變數 DEVICE_ID 就只處理那一台（本機測試用）----
def list_device_ids() -> list[str]:
    """DEVICE_ID env var restricts a run to one device (local testing).
    Otherwise every device in aqua_devices is processed, so a 2nd/3rd
    device starts getting forecasts automatically once it's registered."""
    if DEVICE_ID:
        return [DEVICE_ID]
    rows = sb.table("aqua_devices").select("device_id").execute().data
    return [r["device_id"] for r in rows]


# ---- 【7-2】單一裝置的完整流程：本程式最重要的函式，8 個步驟（①~⑧）依序執行 ----
#   ①載入歷史資料 → ②結算舊預測 → ③整理特徵 → ④基準預測 → ⑤gbm 預測
#   → ⑥存預測 → ⑦異常偵測 → ⑧存異常
def process_device(device_id: str) -> None:
    """單一裝置的完整處理流程（步驟 ①~⑧，各步驟的細節見【2-1】到【6-1】）：
    ①載入歷史資料 -> ②結算舊預測 -> ③整理成特徵 -> ④⑤預測 -> ⑥寫回預測
    -> ⑦異常偵測 -> ⑧寫回異常。"""
    # ---- ① 載入歷史資料（見【2-1】）----
    df = load_history(device_id, LOOKBACK_H)
    if df.empty:
        print(f"[{device_id}] no telemetry yet — collect some data first")
        return

    # ---- ② 結算已到期但還沒比對過的舊預測（見【6-1】；命中率在背景預先算好）----
    settled = settle_due_forecasts(device_id, df)
    if settled:
        print(f"[{device_id}] settled {settled} due forecast(s)")

    # ---- ③ 整理成特徵（補值、算移動平均/標準差；見【2-2】）----
    feats = build_features(df, FEATURE_COLS)
    print(f"[{device_id}] {len(feats)} resampled rows | features = {FEATURE_COLS}")

    # ---- ④ 預測：ewma+drift 基準模型（見【3-1】）----
    forecasts = [forecast(device_id, feats, c, HORIZON_STEPS) for c in FEATURE_COLS]

    # ---- ⑤ 預測：gbm 挑戰模型（獨立抓更長的歷史資料來訓練；見【3-3】）----
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
    # ---- ⑥ 寫回資料庫：預測結果（見【5-1】）----
    save("aqua_forecasts", forecasts)

    # ---- ⑦ 異常偵測：欄位少於 3 個用 z-score【4-1】，3 個以上用 IsolationForest【4-2】，再去重【5-2】----
    if len(FEATURE_COLS) >= 3:
        anomalies = detect_multivariate(device_id, feats, FEATURE_COLS)
    else:
        anomalies = detect_univariate(device_id, feats, FEATURE_COLS)
    anomalies = dedupe_anomalies(device_id, anomalies)
    print(f"  [{device_id}] {len(anomalies)} new anomalies")
    # ---- ⑧ 寫回資料庫：異常紀錄（見【5-1】）----
    save("aqua_anomalies", anomalies)


# ---- 【7-3】程式入口 main()：列出所有裝置，對每台各跑一次 7-2 ----
def main() -> None:
    # 預測工作的進入點（由 worker/ingest.py 每 30 分鐘呼叫一次，也可手動執行）：
    # 列出所有裝置 -> 對每台裝置各跑一次 process_device() 完整流程。
    device_ids = list_device_ids()
    if not device_ids:
        print("no devices registered in aqua_devices")
        return
    for device_id in device_ids:
        process_device(device_id)


if __name__ == "__main__":
    main()
