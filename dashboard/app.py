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
import ssl
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import paho.mqtt.client as mqtt
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
PET_LABEL = {"drop": "水滴", "fish": "魚", "cat": "貓", "panda": "熊貓"}   # value must match firmware's petSkinFromString()
RANGE_HOURS = {"24 小時": 24, "7 天": 168, "30 天": 720}

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")

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
def load_history(device_id: str, hours: int) -> pd.DataFrame:
    raw = hours <= 48
    table = "aqua_telemetry" if raw else "aqua_telemetry_hourly"
    tcol = "ts" if raw else "bucket"
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = (
        sb().table(table).select("*").eq("device_id", device_id)
        .gte(tcol, since).order(tcol).limit(20000).execute().data
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df[tcol] = pd.to_datetime(df[tcol], utc=True, format="ISO8601").dt.tz_convert(TZ)
    return df.set_index(tcol)


@cached(30)
def load_anomalies(device_id: str, limit: int = 50) -> pd.DataFrame:
    rows = (
        sb().table("aqua_anomalies").select("*").eq("device_id", device_id)
        .order("ts", desc=True).limit(limit).execute().data
    )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601").dt.tz_convert(TZ)
    return df


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


def publish_pet_skin(site_id: str, device_id: str, skin: str) -> None:
    """Publish a retained MQTT command the ESP32 applies immediately (no
    reboot) and also persists to its own NVS. Retained so a currently
    offline device picks it up the moment it reconnects. Short-lived
    connection — this only runs inside a button click, not continuously."""
    topic = f"aquaponics/{site_id}/{device_id}/cmd"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dash-{device_id}-{os.getpid()}")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
    client.loop_start()
    client.publish(topic, json.dumps({"pet": skin}), qos=1, retain=True)
    time.sleep(0.3)   # give the publish time to flush before we tear the connection down
    client.loop_stop()
    client.disconnect()


# ---------------------------------------------------------------- 介面
@ui.page("/", title="💧 魚菜共生監控")
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
        "unlocked": not DASH_PASSWORD,
    }

    def device() -> dict:
        return next(d for d in devices if d["device_id"] == state["device_id"])

    # ------------------------------------------------------ 各區塊(可局部刷新)
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
    def pet_section():
        dev = device()
        current_pet = dev.get("pet_skin") or "drop"
        with ui.card().classes("w-full"):
            ui.label("虛擬寵物外觀").classes("font-semibold")
            with ui.row().classes("items-center gap-3"):
                sel = ui.select(dict(PET_LABEL), value=current_pet).classes("w-40")
                if not MQTT_HOST:
                    ui.label("尚未設定 MQTT_HOST 等環境變數，無法從這裡送出變更。").classes(
                        "text-xs text-gray-400"
                    )
                else:
                    site_id = dev.get("site_id", "default")
                    device_id = state["device_id"]

                    async def apply(sel=sel, site_id=site_id, device_id=device_id):
                        try:
                            await run.io_bound(publish_pet_skin, site_id, device_id, sel.value)
                            ui.notify(f"已送出「{PET_LABEL[sel.value]}」，裝置上線後會立刻套用。", type="positive")
                        except Exception as exc:
                            ui.notify(f"送出失敗：{exc}", type="negative")

                    ui.button("套用", on_click=apply)

    @ui.refreshable
    def metrics_section():
        latest = load_latest(state["device_id"])
        if not latest:
            ui.label("尚無感測資料。").classes("text-gray-500")
            return
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for m in METRICS:
                v = latest.get(m)
                if v is None:
                    continue
                suffix = f" {UNIT[m]}" if UNIT[m] else ""
                with ui.card().classes("min-w-[140px] flex-1 items-start"):
                    ui.label(LABEL.get(m, m)).classes("text-sm text-gray-500")
                    ui.label(f"{v}{suffix}").classes("text-2xl font-bold text-sky-700")

    @ui.refreshable
    def history_section():
        hours = RANGE_HOURS[state["range_label"]]
        hist = load_history(state["device_id"], hours)
        if hist.empty:
            ui.label("資料量還不足。").classes("text-gray-500")
            return
        for m in ("temperature", "humidity"):
            col = m if m in hist.columns else (f"{m}_avg" if f"{m}_avg" in hist.columns else None)
            if not col:
                continue
            series = hist[col].dropna()
            unit = f"（{UNIT[m]}）" if UNIT[m] else ""
            ui.label(f"{LABEL[m]}{unit}").classes("text-sm text-gray-500 mt-2")
            # ECharts "time" axis renders labels in UTC. Our index is
            # Asia/Taipei — strip the tz and feed the wall-clock as epoch ms
            # so the axis shows local time on a real (gap-aware) timeline.
            ms = (series.index.tz_localize(None).astype("int64") // 1_000_000).tolist()
            pts = [[int(t), round(float(v), 2)] for t, v in zip(ms, series.values)]
            ui.echart(
                {
                    "grid": {"left": 45, "right": 15, "top": 10, "bottom": 30},
                    "xAxis": {
                        "type": "time",
                        "axisLabel": {"formatter": "{MM}-{dd}\n{HH}:{mm}", "hideOverlap": True},
                    },
                    "yAxis": {"type": "value", "scale": True},
                    "tooltip": {"trigger": "axis"},
                    "series": [
                        {
                            "type": "line",
                            "data": pts,
                            "smooth": True,
                            "showSymbol": False,
                            "areaStyle": {"opacity": 0.15},
                            "color": "#0284c7",
                        }
                    ],
                }
            ).classes("w-full h-48")

    @ui.refreshable
    def anomalies_section():
        an = load_anomalies(state["device_id"])
        if an.empty:
            with ui.row().classes("items-center gap-2 text-green-600"):
                ui.icon("check_circle")
                ui.label("目前沒有異常紀錄。")
            return
        show = an[["ts", "metric", "value", "method", "note"]].copy()
        show["ts"] = show["ts"].dt.strftime("%m-%d %H:%M")
        show["metric"] = show["metric"].map(lambda x: LABEL.get(x, x))
        columns = [
            {"name": "ts", "label": "時間", "field": "ts", "align": "left"},
            {"name": "metric", "label": "項目", "field": "metric", "align": "left"},
            {"name": "value", "label": "數值", "field": "value", "align": "left"},
            {"name": "method", "label": "方法", "field": "method", "align": "left"},
            {"name": "note", "label": "說明", "field": "note", "align": "left"},
        ]
        ui.table(columns=columns, rows=show.to_dict("records"), row_key="ts").classes("w-full")

    @ui.refreshable
    def thresholds_section():
        if DASH_PASSWORD and not state["unlocked"]:
            with ui.row().classes("items-center gap-3"):
                pw = ui.input("編輯密碼", password=True).classes("w-48")

                def try_unlock(pw=pw):
                    if pw.value == DASH_PASSWORD:
                        state["unlocked"] = True
                        thresholds_section.refresh()
                    else:
                        ui.notify("密碼錯誤", type="negative")

                ui.button("解鎖", on_click=try_unlock)
            ui.label("唯讀模式,輸入密碼才能編輯。").classes("text-xs text-gray-400")
            return

        th = load_thresholds(state["device_id"])
        edits: dict = {}
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full font-semibold text-sm"):
                ui.label("項目").classes("w-24")
                ui.label("下限").classes("w-24")
                ui.label("上限").classes("w-24")
                ui.label("啟用").classes("w-16")
            for m in METRICS:
                cur = th.get(m, {})
                with ui.row().classes("w-full items-center"):
                    ui.label(LABEL.get(m, m)).classes("w-24")
                    mn = ui.number(value=float(cur.get("min_val") or 0.0), step=0.5).classes("w-24")
                    mx = ui.number(value=float(cur.get("max_val") or 0.0), step=0.5).classes("w-24")
                    en = ui.checkbox(value=bool(cur.get("enabled", False))).classes("w-16")
                    edits[m] = (mn, mx, en)

            def do_save(edits=edits):
                payload = {m: (mn.value, mx.value, en.value) for m, (mn, mx, en) in edits.items()}
                save_thresholds(state["device_id"], payload)
                ui.notify("已儲存。", type="positive")
                thresholds_section.refresh()

            ui.button("儲存", on_click=do_save)
        ui.label(
            "雲端每分鐘檢查一次(aqua_check_thresholds);超出範圍會出現在上方,方法顯示 threshold。"
        ).classes("text-xs text-gray-400 mt-1")

    # ------------------------------------------------------------- 事件處理
    def refresh_all():
        status_section.refresh()
        pet_section.refresh()
        metrics_section.refresh()
        history_section.refresh()
        anomalies_section.refresh()
        thresholds_section.refresh()

    def on_device_change(e):
        state["device_id"] = e.value
        state["unlocked"] = not DASH_PASSWORD
        refresh_all()

    def on_range_change(e):
        state["range_label"] = e.value
        history_section.refresh()

    def on_refresh_click():
        clear_cache()
        refresh_all()

    # ---------------------------------------------------------------- 排版
    with ui.header().classes("items-center justify-between bg-sky-600 text-white px-4 py-2"):
        ui.label("💧 魚菜共生監控").classes("text-lg font-semibold")
        ui.button(icon="refresh", on_click=on_refresh_click).props("flat round color=white")

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        ui.select(ids, value=state["device_id"], label="裝置", on_change=on_device_change).classes("w-56")
        status_section()
        pet_section()
        metrics_section()

        ui.label("歷史趨勢").classes("text-lg font-semibold mt-2")
        ui.toggle(list(RANGE_HOURS.keys()), value=state["range_label"], on_change=on_range_change)
        history_section()

        ui.label("近期異常").classes("text-lg font-semibold mt-2")
        anomalies_section()

        ui.label("警戒範圍").classes("text-lg font-semibold mt-2")
        thresholds_section()

    # 每 60 秒自動重新整理一次(對齊裝置上傳週期)。
    ui.timer(60.0, on_refresh_click)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        favicon="💧",
        reload=False,
        show=False,
    )
