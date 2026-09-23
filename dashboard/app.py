"""
魚菜共生監控 — NiceGUI 儀表板

從 Supabase 讀取即時 / 歷史感測資料、顯示異常、並可編輯警戒範圍
(aqua_thresholds)。NiceGUI 在 Render 伺服器端執行,Supabase service key
只留在容器環境變數,不會傳到瀏覽器。

本機執行:
    cd dashboard
    cp .env.example .env        # 然後編輯
    pip install -r requirements.txt
    python app.py
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import paho.mqtt.client as mqtt
import pandas as pd
from nicegui import run, ui
from supabase import create_client

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TZ = "Asia/Taipei"
METRICS = ["temperature", "humidity", "water_temp", "ph", "soil_moisture"]
LABEL = {
    "temperature": "氣溫",
    "humidity": "濕度",
    "water_temp": "水溫",
    "ph": "pH",
    "soil_moisture": "土壤濕度",
}
UNIT = {
    "temperature": "°C",
    "humidity": "%RH",
    "water_temp": "°C",
    "ph": "",
    "soil_moisture": "%",
}
COLOR = {
    "temperature": "#ea580c",   # orange
    "humidity": "#0284c7",      # blue
    "water_temp": "#0d9488",    # teal
    "ph": "#7c3aed",            # violet
    "soil_moisture": "#a16207", # amber-brown
}
# keys must match firmware's petSkinFromString()
PET_LABEL = {"drop": "水滴", "fish": "魚", "cat": "貓", "panda": "熊貓"}
RANGE_HOURS = {"24 小時": 24, "7 天": 168, "30 天": 720}
# ~7 days of forecast rows (2 models x 2 metrics x 48 runs/day) — enough
# history for the 「上次預測 vs 實際」date picker to have real choices.
FORECAST_EVAL_LOOKBACK_ROWS = 2 * 2 * 48 * 7
METRIC_FILTERS = {  # 「近期異常」「上次預測 vs 實際」的項目篩選
    "溫度": ["temperature"],
    "濕度": ["humidity"],
    "溫溼度": ["temperature", "humidity"],
}

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
DASH_USERNAME = os.environ.get("DASH_USERNAME", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
# Gate the admin page if EITHER is set, so a half-configured pair (one set,
# one forgotten) still locks the page instead of silently leaving it open.
ADMIN_AUTH_REQUIRED = bool(DASH_USERNAME or DASH_PASSWORD)

_sb = None


def sb():
    global _sb
    if _sb is None:
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    return _sb


# ---------------------------------------------------------------- 簡易快取
# Streamlit 原本靠 st.cache_data(ttl=30) 在多個使用者間共享查詢結果,
# 這裡用一個行程內的字典做等效效果,避免每次畫面刷新都打 Supabase。
_cache: dict[tuple, tuple[float, object]] = {}


def cached(ttl: float):
    def deco(fn):
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            now = time.time()
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            val = fn(*args, **kwargs)
            _cache[key] = (now, val)
            return val

        return wrapper

    return deco


def clear_cache() -> None:
    _cache.clear()


@cached(30)
def load_devices():
    return sb().table("aqua_devices").select("*").order("device_id").execute().data


@cached(30)
def load_latest(device_id: str):
    rows = sb().table("aqua_latest").select("*").eq("device_id", device_id).execute().data
    return rows[0] if rows else None


@cached(30)
def load_all_latest() -> dict:
    """{device_id: latest row} for every device — for the multi-board overview."""
    rows = sb().table("aqua_latest").select("*").execute().data
    return {r["device_id"]: r for r in rows}


@cached(30)
def load_history(device_id: str, hours: int) -> pd.DataFrame:
    raw = hours <= 48
    table = "aqua_telemetry" if raw else "aqua_telemetry_hourly"
    tcol = "ts" if raw else "bucket"
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    # PostgREST caps a response at ~1000 rows. Ascending + limit therefore
    # returned the OLDEST 1000 rows of the window (missing the last ~7 h of a
    # busy day). Page through NEWEST-first instead until the window is covered.
    rows: list = []
    for page in range(30):                       # 30 * 1000 = 30k row ceiling
        chunk = (
            sb().table(table).select("*").eq("device_id", device_id)
            .gte(tcol, since).order(tcol, desc=True)
            .range(page * 1000, page * 1000 + 999).execute().data
        )
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df[tcol] = pd.to_datetime(df[tcol], utc=True, format="ISO8601")
    df = df.set_index(tcol).sort_index()
    if raw:
        # 1 row/min is too dense to plot; bin to 10-min means. Empty bins stay
        # NaN so real gaps (device offline) show as breaks in the line.
        df = df.select_dtypes("number").resample("10min").mean()
    # Return a tz-NAIVE index already holding Asia/Taipei wall-clock, so
    # downstream strftime can't misfire. (pandas 2.x drops the tz on a
    # tz-aware .resample(); pandas 3.x keeps it — normalise both to UTC-naive
    # first, then add the fixed +8 h. Taiwan has no DST, so this is exact.)
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx + pd.Timedelta(hours=8)
    return df


@cached(30)
def load_anomalies(device_id: str, limit: int = 1000) -> pd.DataFrame:
    rows = (
        sb().table("aqua_anomalies").select("*").eq("device_id", device_id)
        .order("ts", desc=True).limit(limit).execute().data
    )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601").dt.tz_convert(TZ)
    return df


@cached(30)
def load_forecasts(device_id: str) -> pd.DataFrame:
    rows = (
        sb().table("aqua_forecasts").select("*").eq("device_id", device_id)
        .order("created_at", desc=True).limit(100).execute().data
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("ts_target", "created_at"):
        df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").dt.tz_convert(TZ)
    return df


@cached(60)
def load_forecast_eval(device_id: str, limit: int = FORECAST_EVAL_LOOKBACK_ROWS) -> pd.DataFrame:
    """Match each already-due forecast to the actual reading nearest its
    target time (within 15 min). Returns a small table for the UI.

    limit defaults to ~7 days of rows: each run writes 2 models x 2 metrics
    = 4 rows every 30 min (192/day), and the dashboard's date picker needs
    more than a single day of history to pick from."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fc = (
        sb().table("aqua_forecasts").select("*").eq("device_id", device_id)
        .lt("ts_target", now_iso).order("ts_target", desc=True).limit(limit).execute().data
    )
    if not fc:
        return pd.DataFrame()
    f = pd.DataFrame(fc)
    f["ts_target"] = pd.to_datetime(f["ts_target"], utc=True, format="ISO8601")
    lo = (f["ts_target"].min() - pd.Timedelta(minutes=15)).isoformat()
    hi = (f["ts_target"].max() + pd.Timedelta(minutes=15)).isoformat()
    # Telemetry lands roughly once a minute, so a wide [lo, hi] window (the
    # date picker can span days) needs a limit sized to match — a flat 1000
    # only covered the most recent ~16h and silently starved older dates of
    # any match.
    span_minutes = (pd.Timestamp(hi) - pd.Timestamp(lo)).total_seconds() / 60
    tel_limit = min(50_000, max(1000, int(span_minutes) + 200))
    tel = (
        sb().table("aqua_telemetry")
        .select("ts,temperature,humidity,water_temp,ph,soil_moisture")
        .eq("device_id", device_id).gte("ts", lo).lte("ts", hi)
        .order("ts", desc=True).limit(tel_limit).execute().data
    )
    t = pd.DataFrame(tel)
    if t.empty:
        return pd.DataFrame()
    t["ts"] = pd.to_datetime(t["ts"], utc=True, format="ISO8601")
    t = t.set_index("ts").sort_index()
    out = []
    for _, r in f.iterrows():
        metric = r["metric"]
        if metric not in t.columns:
            continue
        s = t[metric].dropna()
        if s.empty:
            continue
        pos = s.index.get_indexer([r["ts_target"]], method="nearest")[0]
        if abs((s.index[pos] - r["ts_target"]).total_seconds()) > 900:
            continue
        actual = float(s.iloc[pos])
        yhat = float(r["yhat"])
        lo_v, hi_v = r.get("yhat_lower"), r.get("yhat_upper")
        hit = pd.notna(lo_v) and pd.notna(hi_v) and float(lo_v) <= actual <= float(hi_v)
        ts_local = r["ts_target"].tz_convert(TZ)
        out.append(
            {
                "ts_target": ts_local.strftime("%m-%d %H:%M"),
                "_date": ts_local.strftime("%Y-%m-%d"),
                "metric": LABEL.get(metric, metric),
                "model": r["model"],
                "yhat": round(yhat, 1),
                "actual": round(actual, 1),
                "err": round(abs(yhat - actual), 2),
                "hit": "✓" if hit else "✗",
            }
        )
    return pd.DataFrame(out)


def load_line_paused() -> bool:
    """Not @cached — this is a manual on/off switch the user just clicked
    on this same page, so it must read back exactly what was written."""
    rows = sb().table("aqua_settings").select("value").eq("key", "line_push_paused").execute().data
    return bool(rows and rows[0]["value"])


def set_line_paused(paused: bool) -> None:
    sb().table("aqua_settings").upsert(
        {
            "key": "line_push_paused",
            "value": paused,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="key",
    ).execute()


@cached(30)
def load_thresholds(device_id: str) -> dict:
    rows = sb().table("aqua_thresholds").select("*").eq("device_id", device_id).execute().data
    return {r["metric"]: r for r in rows}


def save_thresholds(device_id: str, edits: dict) -> None:
    payload = [
        {
            "device_id": device_id,
            "metric": m,
            "min_val": mn,
            "max_val": mx,
            "enabled": en,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        for m, (mn, mx, en) in edits.items()
    ]
    sb().table("aqua_thresholds").upsert(payload, on_conflict="device_id,metric").execute()
    clear_cache()


def publish_cmd(site_id: str, device_id: str, payload: dict) -> None:
    """Publish a retained MQTT command the ESP32 applies immediately (no
    reboot) and persists to its own NVS. Retained so a currently offline
    device picks it up on reconnect. Short-lived connection — only runs
    inside a button click, not continuously."""
    topic = f"aquaponics/{site_id}/{device_id}/cmd"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dash-{device_id}-{os.getpid()}")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
    client.loop_start()
    client.publish(topic, json.dumps(payload), qos=1, retain=True)
    time.sleep(0.3)   # let the publish flush before tearing the connection down
    client.loop_stop()
    client.disconnect()


def publish_pet_state(site_id: str, device_id: str, skin: str, hot: float, cold: float) -> None:
    # One retained message carrying the full pet state, so an offline device
    # catches up on everything at once (a partial retained payload would
    # clobber the rest).
    publish_cmd(site_id, device_id, {"pet": skin, "pet_hot": hot, "pet_cold": cold})


# ---------------------------------------------------------------- 介面
@ui.page("/", title="💧 AIoT智慧物聯系統")
def main_page() -> None:
    ui.colors(primary="#0284c7", secondary="#0891b2", accent="#22c55e", positive="#22c55e")

    devices = load_devices()

    if not devices:
        with ui.column().classes("w-full items-center gap-3 p-16"):
            ui.icon("warning", size="xl").classes("text-amber-500")
            ui.label("尚未註冊任何裝置。").classes("text-lg text-gray-500")
        return

    ids = [d["device_id"] for d in devices]
    state = {
        "device_id": ids[0],
        "range_label": "24 小時",
        "anom_metric": "溫溼度",
        # default to today (Asia/Taipei); falls back to 全部 if today has no
        # rows yet (see the date_options guard in anomalies_section)
        "anom_date": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d"),
        "fc_metric": "溫溼度",
        "fc_date": "全部",
    }

    def device() -> dict:
        return next(d for d in devices if d["device_id"] == state["device_id"])

    dev_select = None  # set when the layout is built; kept in sync by select_device

    def select_device(did: str) -> None:
        state["device_id"] = did
        if dev_select is not None:
            dev_select.value = did
        refresh_all()

    def _out_of_band(v, thr_row) -> bool:
        if v is None or not thr_row or not thr_row.get("enabled"):
            return False
        lo, hi = thr_row.get("min_val"), thr_row.get("max_val")
        return (lo is not None and float(v) < float(lo)) or (hi is not None and float(v) > float(hi))

    # ------------------------------------------------------ 各區塊(可局部刷新)
    @ui.refreshable
    def overview_section():
        devs = load_devices()
        if len(devs) < 2:
            return  # 只有一台時不用總覽
        latest_map = load_all_latest()
        ui.label("裝置總覽（點卡片切換下方詳細檢視）").classes("text-lg font-semibold")
        with ui.row().classes("w-full gap-3 flex-wrap"):
            for d in devs:
                did = d["device_id"]
                lt = latest_map.get(did, {})
                thr = load_thresholds(did)
                ls = d.get("last_seen")
                online = bool(
                    ls
                    and (
                        datetime.now(timezone.utc)
                        - pd.to_datetime(ls, utc=True, format="ISO8601")
                    ).total_seconds() < 300
                )
                sel = did == state["device_id"]
                card = ui.card().classes(
                    "min-w-[190px] flex-1 items-start cursor-pointer "
                    + ("border-2 border-sky-500" if sel else "border border-gray-200")
                )
                card.on("click", lambda did=did: select_device(did))
                with card:
                    ui.label(f"{'🟢' if online else '🔴'} {d.get('name') or did}").classes(
                        "text-sm font-medium"
                    )
                    with ui.row().classes("gap-4 items-baseline"):
                        for m in ("temperature", "humidity"):
                            v = lt.get(m)
                            if v is None:
                                continue
                            suffix = f" {UNIT[m]}" if UNIT[m] else ""
                            colour = "text-red-600" if _out_of_band(v, thr.get(m)) else "text-sky-700"
                            ui.label(f"{v}{suffix}").classes(f"text-lg font-bold {colour}")

    @ui.refreshable
    def status_section():
        dev = device()
        last_seen = dev.get("last_seen")
        if not last_seen:
            return
        seen = pd.to_datetime(last_seen, utc=True, format="ISO8601")
        age = (datetime.now(timezone.utc) - seen).total_seconds()
        online = age < 300
        with ui.row().classes("items-center gap-2"):
            ui.icon("circle", size="10px").classes("text-green-500" if online else "text-red-500")
            label = "上線" if online else f"離線（{int(age // 60)} 分鐘）"
            ui.label(f"{label} · 最後上線 {seen.tz_convert(TZ):%Y-%m-%d %H:%M:%S}").classes(
                "text-sm text-gray-500"
            )

    @ui.refreshable
    def metrics_section():
        latest = load_latest(state["device_id"])
        if not latest:
            ui.label("尚無感測資料。").classes("text-gray-500")
            return
        thr = load_thresholds(state["device_id"])
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for m in METRICS:
                v = latest.get(m)
                if v is None:
                    continue
                suffix = f" {UNIT[m]}" if UNIT[m] else ""
                t = thr.get(m) or {}
                out_of_band = _out_of_band(v, t)
                lo, hi = t.get("min_val"), t.get("max_val")
                colour = "text-red-600" if out_of_band else "text-sky-700"
                with ui.card().classes("min-w-[140px] flex-1 items-start"):
                    ui.label(LABEL.get(m, m)).classes("text-sm text-gray-500")
                    ui.label(f"{v}{suffix}").classes(f"text-2xl font-bold {colour}")
                    if out_of_band:
                        ui.label(f"⚠ 超出範圍 [{lo} – {hi}]").classes("text-xs text-red-600")

    @ui.refreshable
    def forecast_section():
        fc = load_forecasts(state["device_id"])
        if fc.empty:
            ui.label(
                "尚無預測。執行 analysis/baseline.py（或等排程）後會出現。"
            ).classes("text-gray-500")
            return
        wanted = METRIC_FILTERS[state["fc_metric"]]
        wanted_labels = [LABEL.get(k, k) for k in wanted]
        # one card per (metric, model) — champion (ewma+drift) and
        # challenger (gbm) run side by side, never replacing each other
        latest = (
            fc[fc["metric"].isin(wanted)]
            .sort_values("created_at")
            .groupby(["metric", "model"])
            .tail(1)
            .sort_values(["metric", "model"])
        )
        # Fixed 2-column grid, not a flex-wrap row — flex-wrap breaks the
        # (metric, model) pairing as soon as a row happens to fit 3 cards
        # instead of 2 (odd number wraps, "氣溫 · gbm" lands alone on the
        # next line, no longer next to "氣溫 · ewma+drift"). Grid always
        # keeps every metric's two model-cards on the same row.
        with ui.grid(columns=2).classes("w-full gap-4"):
            for _, r in latest.iterrows():
                m = r["metric"]
                u = UNIT.get(m, "")
                with ui.card().classes("items-start"):
                    ui.label(
                        f"{LABEL.get(m, m)} · {r['model']} · {int(r['horizon_min'])} 分鐘後"
                    ).classes("text-sm text-gray-500")
                    ui.label(f"{round(float(r['yhat']), 1)} {u}".strip()).classes(
                        "text-2xl font-bold text-indigo-700"
                    )
                    lo_v, hi_v = r.get("yhat_lower"), r.get("yhat_upper")
                    if pd.notna(lo_v) and pd.notna(hi_v):
                        ui.label(
                            f"可能範圍 {round(float(lo_v), 1)} – {round(float(hi_v), 1)}"
                        ).classes("text-xs text-gray-400")
        newest = fc["created_at"].max()
        models = "、".join(sorted(fc["model"].unique()))
        ui.label(f"模型:{models} · 產生於 {newest:%m-%d %H:%M}").classes("text-xs text-gray-400")

        ev_all = load_forecast_eval(state["device_id"])
        if not ev_all.empty:
            ev_all = ev_all[ev_all["metric"].isin(wanted_labels)]
        if ev_all.empty:
            return
        ui.label("上次預測 vs 實際").classes("text-sm text-gray-500 mt-3")

        dates = sorted(ev_all["_date"].unique(), reverse=True)
        date_options = ["全部"] + dates
        if state["fc_date"] not in date_options:
            state["fc_date"] = "全部"

        def on_fc_date_change(e):
            state["fc_date"] = e.value
            forecast_section.refresh()

        ui.select(
            date_options, value=state["fc_date"], label="日期", on_change=on_fc_date_change
        ).classes("w-40")

        ev = ev_all if state["fc_date"] == "全部" else ev_all[ev_all["_date"] == state["fc_date"]]
        if ev.empty:
            ui.label("這天沒有可比對的預測資料。").classes("text-xs text-gray-400")
            return
        cols = [
            {"name": "ts_target", "label": "目標時間", "field": "ts_target", "align": "left"},
            {"name": "metric", "label": "項目", "field": "metric", "align": "left"},
            {"name": "model", "label": "模型", "field": "model", "align": "left"},
            {"name": "yhat", "label": "預測", "field": "yhat", "align": "left"},
            {"name": "actual", "label": "實際", "field": "actual", "align": "left"},
            {"name": "err", "label": "誤差", "field": "err", "align": "left"},
            {"name": "hit", "label": "命中", "field": "hit", "align": "left"},
        ]
        # ts_target alone can repeat across rows now (both models predict the
        # same target time each run) — row_key needs a value unique per row.
        rows = ev.reset_index(drop=True).reset_index(names="_row_id").to_dict("records")
        ui.table(columns=cols, rows=rows, row_key="_row_id").classes("w-full")
        with ui.column().classes("gap-0.5 mt-1"):
            for m in METRICS:
                label = LABEL.get(m, m)
                g_metric = ev[ev["metric"] == label]
                if g_metric.empty:
                    continue
                for model_name in sorted(g_metric["model"].unique()):
                    g = g_metric[g_metric["model"] == model_name]
                    ui.label(
                        f"{label} · {model_name} · 近 {len(g)} 筆 · "
                        f"命中率 {(g['hit'] == '✓').mean() * 100:.0f}% · "
                        f"平均誤差 {g['err'].mean():.2f}"
                    ).classes("text-xs text-gray-400")

    @ui.refreshable
    def history_section():
        hours = RANGE_HOURS[state["range_label"]]
        hist = load_history(state["device_id"], hours)
        if hist.empty:
            ui.label("資料量還不足。").classes("text-gray-500")
            return
        ui.label(
            f"資料範圍 {hist.index[0]:%m-%d %H:%M} ～ {hist.index[-1]:%m-%d %H:%M}"
            f"（{len(hist)} 點）"
        ).classes("text-xs text-gray-400")
        thr = load_thresholds(state["device_id"])
        for m in ("temperature", "humidity"):
            col = m if m in hist.columns else (f"{m}_avg" if f"{m}_avg" in hist.columns else None)
            if not col:
                continue
            series = hist[col]                       # keep NaN rows so gaps show
            unit = f"（{UNIT[m]}）" if UNIT[m] else ""
            ui.label(f"{LABEL[m]}{unit}").classes("text-sm text-gray-500 mt-2")
            # Category axis with labels formatted straight from the Asia/Taipei
            # index — no ECharts timezone interpretation to get wrong. Bins are
            # evenly spaced (10 min raw / 1 h rollup) so it still reads as a
            # timeline. A fixed "show every Nth label" step was tuned for a
            # desktop-width chart and overlapped into unreadable mush on a
            # phone-width one — let ECharts measure the actual rendered width
            # and thin + rotate labels itself instead.
            #
            # The tick spacing itself has to depend on the selected range: 6h
            # boundaries (00/06/12/18) read fine over a single day, but the
            # same rule over 7 or 30 days puts a label on every one of those
            # boundaries across the whole window (28+ for 7 days) and the
            # axis turns into a solid wall of text. Widen to one tick/day (or
            # every 3rd day for 30 days) once the window is longer than a day.
            labels = [t.strftime("%m-%d %H:%M") for t in series.index]
            vals = [None if pd.isna(v) else round(float(v), 2) for v in series.values]

            # Overlay the 警戒範圍 upper/lower bounds as dashed reference
            # lines, so a breach is visible on the chart, not just in the
            # anomalies list. Only for an enabled threshold with a set bound.
            t = thr.get(m) or {}
            mark_lines = []
            if t.get("enabled"):
                if t.get("min_val") is not None:
                    mark_lines.append({"name": "下限", "yAxis": float(t["min_val"])})
                if t.get("max_val") is not None:
                    mark_lines.append({"name": "上限", "yAxis": float(t["max_val"])})

            series_def = {
                "type": "line",
                "data": vals,
                "connectNulls": False,
                "smooth": True,
                "showSymbol": False,
                "areaStyle": {"opacity": 0.15},
                "color": COLOR.get(m, "#0284c7"),
            }
            # ECharts' yAxis scale:true only looks at the SERIES data, not
            # markLine values — a threshold far from the current reading
            # (e.g. humidity sitting at 67-72 with a 40-60 band) fell outside
            # the auto-scaled range and the line was silently clipped. When
            # there's a threshold line, size the axis to cover data + bounds
            # explicitly instead of leaving it to auto-scale.
            if mark_lines:
                nums = [v for v in vals if v is not None] + [ml["yAxis"] for ml in mark_lines]
                y_lo, y_hi = min(nums), max(nums)
                pad = (y_hi - y_lo) * 0.08 or 1
                y_axis = {"type": "value", "min": round(y_lo - pad, 2), "max": round(y_hi + pad, 2)}
                series_def["markLine"] = {
                    "symbol": "none",
                    "silent": True,
                    "lineStyle": {"color": "#dc2626", "type": "dashed", "width": 1},
                    "label": {
                        "formatter": "{b} {c}",
                        "color": "#dc2626",
                        "fontSize": 10,
                        "position": "insideEndTop",
                    },
                    "data": mark_lines,
                }
            else:
                y_axis = {"type": "value", "scale": True}

            if hours <= 24:
                # Ticks on the 6-hour boundaries (00/06/12/18); full date+time.
                tick_formatter = (
                    "function(value){"
                    "var m=/(\\d{2}):(\\d{2})$/.exec(value);"
                    "if(!m)return '';"
                    "var h=parseInt(m[1],10),mi=parseInt(m[2],10);"
                    "return (mi===0&&h%6===0)?value:'';"
                    "}"
                )
            else:
                # One tick per day (7-day view) or every 3rd day (30-day
                # view), always at midnight; show just the date since the
                # time is always 00:00.
                day_step = 1 if hours <= 168 else 3
                tick_formatter = (
                    "function(value){"
                    "var m=/^(\\d{2})-(\\d{2}) (\\d{2}):(\\d{2})$/.exec(value);"
                    "if(!m)return '';"
                    "var d=parseInt(m[2],10),h=parseInt(m[3],10),mi=parseInt(m[4],10);"
                    f"return (mi===0&&h===0&&d%{day_step}===0)?value.slice(0,5):'';"
                    "}"
                )

            ui.echart(
                {
                    "grid": {"left": 45, "right": 40, "top": 10, "bottom": 50},
                    "xAxis": {
                        "type": "category",
                        "data": labels,
                        "axisLabel": {
                            # index-based thinning drifts off clean hours
                            # since the window rarely starts on one; filter
                            # by the label's own timestamp instead (see
                            # tick_formatter above). NiceGUI evaluates a
                            # ":"-prefixed value as JS (see
                            # dynamic_properties.js / echart.js).
                            "interval": 0,
                            "hideOverlap": True,
                            "rotate": 30,
                            "fontSize": 10,
                            ":formatter": tick_formatter,
                        },
                    },
                    "yAxis": y_axis,
                    "tooltip": {"trigger": "axis"},
                    "series": [series_def],
                }
            ).classes("w-full h-48")

    @ui.refreshable
    def anomalies_section():
        an = load_anomalies(state["device_id"])
        wanted = METRIC_FILTERS[state["anom_metric"]]
        if not an.empty:
            an = an[an["metric"].isin(wanted)]
        if not an.empty:
            # 一小時一筆:同一(項目, 方法, 整點)只保留最新那筆
            an = an.assign(_hour=an["ts"].dt.floor("h"))
            an = (
                an.sort_values("ts", ascending=False)
                .drop_duplicates(subset=["metric", "method", "_hour"])
            )

        # 日期篩選:選項跟著目前(項目篩選後)實際有資料的日期走
        dates = sorted(an["ts"].dt.strftime("%Y-%m-%d").unique(), reverse=True) if not an.empty else []
        date_options = ["全部"] + dates
        if state["anom_date"] not in date_options:
            state["anom_date"] = "全部"

        def on_date_change(e):
            state["anom_date"] = e.value
            anomalies_section.refresh()

        ui.select(date_options, value=state["anom_date"], label="日期", on_change=on_date_change).classes(
            "w-40"
        )
        ui.label("每個整點最多一筆").classes("text-xs text-gray-400")
        if state["anom_date"] != "全部" and not an.empty:
            an = an[an["ts"].dt.strftime("%Y-%m-%d") == state["anom_date"]]

        if an.empty:
            with ui.row().classes("items-center gap-2 text-green-600"):
                ui.icon("check_circle")
                ui.label("目前沒有異常紀錄。")
            return
        method_label = {
            "threshold": "超出範圍",
            "rolling_zscore": "統計偏離",
            "isolation_forest": "多變量偵測",
        }
        show = an[["ts", "metric", "value", "method", "note"]].copy()
        show["ts"] = show["ts"].dt.strftime("%m-%d %H:%M")
        show["metric"] = show["metric"].map(lambda x: LABEL.get(x, x))
        show["method"] = show["method"].map(lambda x: method_label.get(x, x))
        columns = [
            {"name": "ts", "label": "時間", "field": "ts", "align": "left"},
            {"name": "metric", "label": "項目", "field": "metric", "align": "left"},
            {"name": "value", "label": "數值", "field": "value", "align": "left"},
            {"name": "method", "label": "方法", "field": "method", "align": "left"},
            {"name": "note", "label": "說明", "field": "note", "align": "left"},
        ]
        ui.table(columns=columns, rows=show.to_dict("records"), row_key="ts").classes("w-full")

    # ------------------------------------------------------------- 事件處理
    def refresh_all():
        overview_section.refresh()
        status_section.refresh()
        metrics_section.refresh()
        forecast_section.refresh()
        history_section.refresh()
        anomalies_section.refresh()

    def on_device_change(e):
        state["device_id"] = e.value
        refresh_all()

    def on_range_change(e):
        state["range_label"] = e.value
        history_section.refresh()

    def on_anom_metric_change(e):
        state["anom_metric"] = e.value
        anomalies_section.refresh()

    def on_fc_metric_change(e):
        state["fc_metric"] = e.value
        forecast_section.refresh()

    def on_refresh_click():
        clear_cache()
        refresh_all()

    # ---------------------------------------------------------------- 排版
    with ui.header().classes("items-center justify-between bg-sky-600 text-white px-4 py-2"):
        ui.label("💧 AIoT智慧物聯系統").classes("text-lg font-semibold")
        with ui.row().classes("items-center gap-1"):
            ui.button(
                icon="settings",
                # carry the currently-viewed device over, so the admin page
                # doesn't reset back to the first device in the list
                on_click=lambda: ui.navigate.to(f"/admin?device={quote(state['device_id'])}"),
            ).props("flat round color=white").tooltip("AIoT智慧物聯管理後台")
            ui.button(icon="refresh", on_click=on_refresh_click).props("flat round color=white")

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        overview_section()
        dev_select = ui.select(
            ids, value=state["device_id"], label="詳細檢視裝置", on_change=on_device_change
        ).classes("w-56")
        status_section()
        metrics_section()

        ui.label("AI 預測").classes("text-lg font-semibold mt-2")
        ui.toggle(
            list(METRIC_FILTERS.keys()),
            value=state["fc_metric"],
            on_change=on_fc_metric_change,
        )
        forecast_section()

        ui.label("歷史趨勢").classes("text-lg font-semibold mt-2")
        ui.toggle(list(RANGE_HOURS.keys()), value=state["range_label"], on_change=on_range_change)
        history_section()

        ui.label("近期異常").classes("text-lg font-semibold mt-2")
        ui.toggle(
            list(METRIC_FILTERS.keys()),
            value=state["anom_metric"],
            on_change=on_anom_metric_change,
        )
        anomalies_section()

    # 每 60 秒自動重新整理一次(對齊裝置上傳週期)。
    ui.timer(60.0, on_refresh_click)


# ---------------------------------------------------------------- 管理後台
@ui.page("/admin", title="⚙️ AIoT智慧物聯管理後台")
def admin_page(device: str = "") -> None:
    """所有需要設定/會改變裝置或雲端行為的功能都集中在這裡:推播設定、
    顯示設定(OLED 虛擬寵物)、警戒設定。主頁維持純檢視,不放任何設定。

    `device` is an optional ?device=... query param the main page's gear
    icon carries over, so switching to admin doesn't reset back to the
    first device in the list — pages are independent NiceGUI sessions with
    no shared state, so this query param is the only link between them."""
    ui.colors(primary="#0284c7", secondary="#0891b2", accent="#22c55e", positive="#22c55e")

    devices = load_devices()
    if not devices:
        with ui.column().classes("w-full items-center gap-3 p-16"):
            ui.icon("warning", size="xl").classes("text-amber-500")
            ui.label("尚未註冊任何裝置。").classes("text-lg text-gray-500")
        return

    ids = [d["device_id"] for d in devices]
    state = {"device_id": device if device in ids else ids[0], "unlocked": not ADMIN_AUTH_REQUIRED}

    def device() -> dict:
        return next(d for d in devices if d["device_id"] == state["device_id"])

    @ui.refreshable
    def notify_section():
        paused = load_line_paused()

        def on_toggle(e):
            set_line_paused(e.value)
            ui.notify(
                "已暫停 LINE 推播" if e.value else "已恢復 LINE 推播",
                type="warning" if e.value else "positive",
            )
            notify_section.refresh()

        with ui.row().classes("items-center gap-2"):
            ui.switch("暫停 LINE 推播", value=paused, on_change=on_toggle)
            if paused:
                ui.icon("notifications_off").classes("text-amber-500")
        ui.label("關閉後,異常仍會被記錄(不會累積成之後的洗版),只是不會真的推播到 LINE。").classes(
            "text-xs text-gray-400"
        )

    @ui.refreshable
    def display_section():
        dev = device()
        current_pet = dev.get("pet_skin") or "drop"
        hot0 = float(dev.get("pet_hot") if dev.get("pet_hot") is not None else 28)
        cold0 = float(dev.get("pet_cold") if dev.get("pet_cold") is not None else 18)
        with ui.card().classes("w-full"):
            ui.label("OLED 虛擬寵物").classes("font-semibold")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                sel = ui.select(dict(PET_LABEL), value=current_pet, label="外觀").classes("w-32")
                n_hot = ui.number(label="流汗門檻 °C", value=hot0, step=0.5).classes("w-32")
                n_cold = ui.number(label="發抖門檻 °C", value=cold0, step=0.5).classes("w-32")
            if not MQTT_HOST:
                ui.label("尚未設定 MQTT_HOST 等環境變數，無法從這裡送出變更。").classes(
                    "text-xs text-gray-400"
                )
            else:
                site_id = dev.get("site_id", "default")
                device_id = state["device_id"]

                async def apply(sel=sel, n_hot=n_hot, n_cold=n_cold, site_id=site_id, device_id=device_id):
                    hot, cold = float(n_hot.value), float(n_cold.value)
                    if cold >= hot:
                        ui.notify("發抖門檻要小於流汗門檻。", type="negative")
                        return
                    try:
                        await run.io_bound(
                            publish_pet_state, site_id, device_id, sel.value, hot, cold
                        )
                        ui.notify(
                            f"已送出：{PET_LABEL[sel.value]}、>{hot}°C 流汗、<{cold}°C 發抖。"
                            "裝置上線後立即套用。",
                            type="positive",
                        )
                    except Exception as exc:
                        ui.notify(f"送出失敗：{exc}", type="negative")

                ui.button("套用", on_click=apply).classes("mt-1")
                ui.label("OLED 寵物表情的門檻(與雲端「警戒設定」告警無關)。").classes(
                    "text-xs text-gray-400"
                )

    @ui.refreshable
    def alert_section():
        th = load_thresholds(state["device_id"])
        edits: dict = {}
        # ui.grid (CSS grid, minmax(0,1fr) columns) shrinks correctly on narrow
        # screens; a ui.row (flexbox) does not — its items refuse to shrink
        # below their natural content width and wrap into a ragged 2-line mess.
        with ui.card().classes("w-full"):
            with ui.grid(columns=4).classes("w-full gap-x-2 gap-y-2 items-center"):
                ui.label("項目").classes("font-semibold text-sm")
                ui.label("下限").classes("font-semibold text-sm")
                ui.label("上限").classes("font-semibold text-sm")
                ui.label("啟用").classes("font-semibold text-sm")
                for m in METRICS:
                    cur = th.get(m, {})
                    ui.label(LABEL.get(m, m)).classes("text-sm")
                    mn = ui.number(value=float(cur.get("min_val") or 0.0), step=0.5).classes("w-full")
                    mx = ui.number(value=float(cur.get("max_val") or 0.0), step=0.5).classes("w-full")
                    en = ui.checkbox(value=bool(cur.get("enabled", False)))
                    edits[m] = (mn, mx, en)

            def do_save(edits=edits):
                payload = {m: (mn.value, mx.value, en.value) for m, (mn, mx, en) in edits.items()}
                save_thresholds(state["device_id"], payload)
                ui.notify("已儲存。", type="positive")
                alert_section.refresh()

            ui.button("儲存", on_click=do_save)
        ui.label(
            "雲端每分鐘檢查一次(aqua_check_thresholds),持續超標最多每小時記一筆,"
            "顯示在主頁「近期異常」的「超出範圍」。"
        ).classes("text-xs text-gray-400 mt-1")

    def on_device_change(e):
        state["device_id"] = e.value
        display_section.refresh()
        alert_section.refresh()

    @ui.refreshable
    def gate_or_content():
        if ADMIN_AUTH_REQUIRED and not state["unlocked"]:
            with ui.card().classes("w-full max-w-sm mx-auto mt-10 items-center gap-3 p-6"):
                ui.icon("lock", size="xl").classes("text-gray-400")
                ui.label("AIoT智慧物聯管理後台登入").classes("text-lg font-semibold")
                user = ui.input("帳號").classes("w-full")
                pw = ui.input("密碼", password=True).classes("w-full")

                def try_unlock(user=user, pw=pw):
                    # compare_digest avoids leaking a match via response-time
                    # differences; not that it matters much for a personal
                    # dashboard, but it's free.
                    ok = secrets.compare_digest(
                        user.value, DASH_USERNAME
                    ) and secrets.compare_digest(pw.value, DASH_PASSWORD)
                    if ok:
                        state["unlocked"] = True
                        gate_or_content.refresh()
                    else:
                        ui.notify("帳號或密碼錯誤", type="negative")

                pw.on("keydown.enter", try_unlock)
                ui.button("登入", on_click=try_unlock).classes("w-full").props("color=primary")
            return

        ui.select(
            ids, value=state["device_id"], label="設定裝置", on_change=on_device_change
        ).classes("w-56")

        ui.label("推播設定").classes("text-lg font-semibold mt-2")
        notify_section()

        ui.label("顯示設定").classes("text-lg font-semibold mt-2")
        display_section()

        ui.label("警戒設定").classes("text-lg font-semibold mt-2")
        alert_section()

    with ui.header().classes("items-center justify-between bg-slate-700 text-white px-4 py-2"):
        ui.label("⚙️ AIoT智慧物聯管理後台").classes("text-lg font-semibold")
        ui.button("← 返回主頁", on_click=lambda: ui.navigate.to("/")).props("flat color=white")

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        gate_or_content()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        favicon="💧",
        reload=False,
        show=False,
    )
