"""
魚菜共生監控 — Streamlit 儀表板

從 Supabase 讀取即時 / 歷史感測資料、顯示異常、並可編輯警戒範圍
(aqua_thresholds)。Streamlit 在 Render 伺服器端執行,Supabase service key
只留在容器環境變數,不會傳到瀏覽器。

本機執行:
    cd dashboard
    cp .env.example .env        # 然後編輯
    pip install -r requirements.txt
    python -m streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
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

st.set_page_config(page_title="魚菜共生監控", page_icon="\U0001f4a7", layout="centered")

# 每 60 秒自動重跑一次(對齊裝置上傳週期)。沒安裝套件時就略過。
try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=60_000, key="auto")
except ImportError:
    pass


@st.cache_resource
def _sb():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


@st.cache_data(ttl=30)
def load_devices():
    return _sb().table("aqua_devices").select("*").order("device_id").execute().data


@st.cache_data(ttl=30)
def load_latest(device_id: str):
    rows = _sb().table("aqua_latest").select("*").eq("device_id", device_id).execute().data
    return rows[0] if rows else None


@st.cache_data(ttl=30)
def load_history(device_id: str, hours: int) -> pd.DataFrame:
    raw = hours <= 48
    table = "aqua_telemetry" if raw else "aqua_telemetry_hourly"
    tcol = "ts" if raw else "bucket"
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = (
        _sb().table(table).select("*").eq("device_id", device_id)
        .gte(tcol, since).order(tcol).limit(20000).execute().data
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df[tcol] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert(TZ)
    return df.set_index(tcol)


@st.cache_data(ttl=30)
def load_anomalies(device_id: str, limit: int = 50) -> pd.DataFrame:
    rows = (
        _sb().table("aqua_anomalies").select("*").eq("device_id", device_id)
        .order("ts", desc=True).limit(limit).execute().data
    )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
    return df


@st.cache_data(ttl=30)
def load_thresholds(device_id: str) -> dict:
    rows = _sb().table("aqua_thresholds").select("*").eq("device_id", device_id).execute().data
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
    _sb().table("aqua_thresholds").upsert(payload, on_conflict="device_id,metric").execute()


# ---------------------------------------------------------------- 介面
st.title("\U0001f4a7 魚菜共生監控")

devices = load_devices()
if not devices:
    st.warning("尚未註冊任何裝置。")
    st.stop()

ids = [d["device_id"] for d in devices]
device_id = st.selectbox("裝置", ids)
dev = next(d for d in devices if d["device_id"] == device_id)

if st.button("\U0001f504 重新整理"):
    st.cache_data.clear()
    st.rerun()

# --- 連線狀態 ---
last_seen = dev.get("last_seen")
if last_seen:
    seen = pd.to_datetime(last_seen, utc=True)
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    badge = "\U0001f7e2 上線" if age < 300 else f"\U0001f534 離線({int(age // 60)} 分鐘)"
    st.caption(f"{badge}  ·  最後上線 {seen.tz_convert(TZ):%Y-%m-%d %H:%M:%S}")

# --- 即時數值 ---
latest = load_latest(device_id)
if latest:
    live = [(m, latest.get(m)) for m in METRICS if latest.get(m) is not None]
    cols = st.columns(min(len(live), 2) or 1)
    for i, (m, v) in enumerate(live):
        suffix = f" {UNIT[m]}" if UNIT[m] else ""
        cols[i % len(cols)].metric(LABEL.get(m, m), f"{v}{suffix}")
else:
    st.info("尚無感測資料。")

# --- 歷史趨勢 ---
st.subheader("歷史趨勢")
rng = st.radio("區間", ["24 小時", "7 天", "30 天"], horizontal=True)
hours = {"24 小時": 24, "7 天": 168, "30 天": 720}[rng]
hist = load_history(device_id, hours)
if hist.empty:
    st.info("資料量還不足。")
else:
    for m in ("temperature", "humidity"):
        col = m if m in hist.columns else (f"{m}_avg" if f"{m}_avg" in hist.columns else None)
        if col:
            unit = f"（{UNIT[m]}）" if UNIT[m] else ""
            st.caption(f"{LABEL[m]}{unit}")
            st.line_chart(hist[[col]].rename(columns={col: LABEL[m]}), height=180)

# --- 近期異常 ---
st.subheader("近期異常")
an = load_anomalies(device_id)
if an.empty:
    st.success("目前沒有異常紀錄。")
else:
    show = an[["ts", "metric", "value", "method", "note"]].copy()
    show["ts"] = show["ts"].dt.strftime("%m-%d %H:%M")
    show["metric"] = show["metric"].map(lambda x: LABEL.get(x, x))
    show = show.rename(
        columns={"ts": "時間", "metric": "項目", "value": "數值", "method": "方法", "note": "說明"}
    )
    st.dataframe(show, hide_index=True, use_container_width=True)

# --- 警戒範圍 ---
st.subheader("警戒範圍")
pw = os.environ.get("DASH_PASSWORD", "")
unlocked = True
if pw:
    unlocked = st.text_input("編輯密碼", type="password") == pw
    if not unlocked:
        st.caption("唯讀模式,輸入密碼才能編輯。")

th = load_thresholds(device_id)
with st.form("thr"):
    hdr = st.columns([3, 2, 2, 1])
    hdr[0].markdown("**項目**")
    hdr[1].markdown("**下限**")
    hdr[2].markdown("**上限**")
    hdr[3].markdown("**啟用**")
    edits: dict = {}
    for m in METRICS:
        cur = th.get(m, {})
        c = st.columns([3, 2, 2, 1])
        c[0].write(LABEL.get(m, m))
        mn = c[1].number_input(
            "min", key=f"mn_{m}", value=float(cur.get("min_val") or 0.0),
            step=0.5, label_visibility="collapsed", disabled=not unlocked,
        )
        mx = c[2].number_input(
            "max", key=f"mx_{m}", value=float(cur.get("max_val") or 0.0),
            step=0.5, label_visibility="collapsed", disabled=not unlocked,
        )
        en = c[3].checkbox(
            "on", key=f"en_{m}", value=bool(cur.get("enabled", False)),
            label_visibility="collapsed", disabled=not unlocked,
        )
        edits[m] = (mn, mx, en)
    if st.form_submit_button("儲存", disabled=not unlocked):
        save_thresholds(device_id, edits)
        st.cache_data.clear()
        st.success("已儲存。")
        st.rerun()

st.caption("雲端每分鐘檢查一次(aqua_check_thresholds);超出範圍會出現在上方,方法顯示 threshold。")
