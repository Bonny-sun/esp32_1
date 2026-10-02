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

import asyncio
import json
import os
import secrets
import ssl
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import paho.mqtt.client as mqtt
import pandas as pd
from nicegui import app, run, ui
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
ICON = {
    "temperature": "thermostat",
    "humidity": "water_drop",
    "water_temp": "set_meal",
    "ph": "science",
    "soil_moisture": "grass",
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
# Shared "functional block" card look for both the main page and admin page.
# `flat` drops Quasar's default elevation shadow so only this Tailwind
# shadow applies (no stacked/doubled shadow) — rounded corners + a soft
# shadow instead of the old flat hard border, for a more modern card look.
SECTION_CARD_CLASSES = "w-full rounded-xl shadow-md border border-gray-100"
# (label, route, icon) in the order they appear in nav_bar() — icon is a
# Material Symbols name (Quasar's default icon set), used on both the
# desktop nav row and the mobile bottom tab bar.
NAV_LINKS = [
    ("首頁", "/", "home"),
    ("異常警戒", "/anomalies", "warning"),
    ("AI預測", "/forecast", "insights"),
    ("後台設定", "/admin", "settings"),
]

# ---------------------------------------------------------------- 主題
# Switchable look, stored per-browser (app.storage.user, a signed cookie —
# see STORAGE_SECRET). Deliberately NOT implemented by threading a second
# set of Tailwind classes through every ui.label()/ui.card() call in this
# file (hundreds of call sites, most with no explicit color class at all,
# relying on the browser/Quasar default) — instead one global stylesheet,
# scoped under a `theme-tech` class nav_bar() puts on <body>, overrides the
# small, consistent set of Tailwind/Quasar classes this app actually uses
# (card backgrounds via Quasar's own `.q-card`, the handful of text-gray-*
# shades, borders, form fields, tables). Adding a theme later means adding
# another `theme-<name>` block here, not touching every page function.
# `!important` throughout: this NiceGUI version's bundled Tailwind runtime
# doesn't reliably let a later same-specificity rule win by source order
# alone (see the `max-md:hidden` vs `hidden md:flex` finding from the same
# session) — important sidesteps that gamble entirely.
THEMES = {"classic": "經典藍", "tech": "科技感"}
DEFAULT_THEME = "classic"
THEME_CSS = """
body { background-color: #eef6fc !important; }

body.theme-tech { background-color: #0b1220 !important; color: #e2e8f0 !important; }
body.theme-tech .q-card {
    background-color: #121a35 !important;
    border-color: #1e3a5f !important;
    box-shadow: 0 0 14px rgba(56, 189, 248, 0.18) !important;
}
body.theme-tech .q-page, body.theme-tech .q-layout, body.theme-tech .q-page-container {
    color: inherit !important;
}
body.theme-tech .text-gray-800 { color: #e2e8f0 !important; }
body.theme-tech .text-gray-700 { color: #cbd5e1 !important; }
body.theme-tech .text-gray-600,
body.theme-tech .text-gray-500,
body.theme-tech .text-gray-400 { color: #94a3b8 !important; }
body.theme-tech .border-gray-100,
body.theme-tech .border-gray-200,
body.theme-tech .border-gray-400 { border-color: #1e3a5f !important; }
body.theme-tech .bg-gray-50 { background-color: #121a35 !important; }
body.theme-tech .bg-sky-50 { background-color: #0f1b3d !important; }
body.theme-tech .bg-sky-100 { background-color: #16204a !important; }
body.theme-tech .border-sky-100,
body.theme-tech .border-sky-600 { border-color: #1e3a5f !important; }
body.theme-tech .text-sky-700,
body.theme-tech .text-sky-600 { color: #7dd3fc !important; }
body.theme-tech .q-drawer {
    background-color: #121a35 !important;
    border-color: #1e3a5f !important;
}
body.theme-tech .bg-amber-100 { background-color: #3a2a0f !important; }
body.theme-tech .text-amber-700 { color: #fbbf24 !important; }
body.theme-tech .q-field__control,
body.theme-tech .q-field__native,
body.theme-tech .q-field__label,
body.theme-tech .q-field__marginal { background-color: transparent !important; color: #e2e8f0 !important; }
body.theme-tech table, body.theme-tech .q-table,
body.theme-tech .q-table__container { background-color: #121a35 !important; color: #e2e8f0 !important; }
body.theme-tech .q-table tbody tr:nth-child(even) { background-color: #16204a !important; }
"""


def get_theme() -> str:
    try:
        return app.storage.user.get("theme", DEFAULT_THEME)
    except Exception:
        return DEFAULT_THEME


def set_theme(name: str) -> None:
    app.storage.user["theme"] = name


ui.add_head_html(f"<style>{THEME_CSS}</style>", shared=True)
RANGE_HOURS = {"24 小時": 24, "7 天": 168, "30 天": 720}
# ~7 days of forecast rows (2 models x 2 metrics x 48 runs/day) — enough
# history for the 「上次預測 vs 實際」date picker to have real choices.
FORECAST_EVAL_LOOKBACK_ROWS = 2 * 2 * 48 * 7
METRIC_FILTERS = {  # 「近期異常」「上次預測 vs 實際」的項目篩選
    "溫度": ["temperature"],
    "濕度": ["humidity"],
    "溫溼度": ["temperature", "humidity"],
}

# AI 預測總表 row order within each (device, metric) group: gbm (the more
# accurate challenger model) listed above ewma+drift (the baseline); any
# future model name not in this list sorts alphabetically after both.
MODEL_ORDER = {"gbm": 0, "ewma+drift": 1}


def _model_sort_key(model_name: str):
    return (MODEL_ORDER.get(model_name, 99), model_name)

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
DASH_USERNAME = os.environ.get("DASH_USERNAME", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
# Only signs the browser-side cookie that remembers each visitor's chosen
# theme (app.storage.user) — not a secret in the security sense, so a
# stable default is fine; an operator can still override it.
STORAGE_SECRET = os.environ.get("STORAGE_SECRET", "aquaponics-dashboard-theme-prefs")
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
def load_anomalies(device_id: str | None, limit: int = 1000) -> pd.DataFrame:
    """device_id=None fetches across every device (for a fleet-wide alarm
    list) instead of filtering to one."""
    q = sb().table("aqua_anomalies").select("*")
    if device_id is not None:
        q = q.eq("device_id", device_id)
    rows = q.order("ts", desc=True).limit(limit).execute().data
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
    # date picker's range grows for as long as ewma+drift has been running —
    # weeks, not days) can hold more rows than any single guessed limit
    # safely covers. A flat 1000, then a "span_minutes + margin" estimate,
    # both silently truncated to the newest slice of the window once the
    # real row count crept past the guess, starving older dates of any
    # match even though the telemetry existed. Page through NEWEST-first
    # instead — the same fix, and the same PostgREST page-size limit, as
    # baseline.py's load_history() — so this can never undershoot.
    tel_rows: list = []
    for page in range(60):  # 60 * 1000 = 60k row ceiling
        chunk = (
            sb().table("aqua_telemetry")
            .select("ts,temperature,humidity,water_temp,ph,soil_moisture")
            .eq("device_id", device_id).gte("ts", lo).lte("ts", hi)
            .order("ts", desc=True)
            .range(page * 1000, page * 1000 + 999)
            .execute().data
        )
        tel_rows.extend(chunk)
        if len(chunk) < 1000:
            break
    t = pd.DataFrame(tel_rows)
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


def load_global_line_paused() -> bool:
    """Not @cached — this is a manual on/off switch the user just clicked
    on this same page, so it must read back exactly what was written."""
    rows = sb().table("aqua_settings").select("value").eq("key", "line_push_paused").execute().data
    return bool(rows and rows[0]["value"])


def set_global_line_paused(paused: bool) -> None:
    sb().table("aqua_settings").upsert(
        {
            "key": "line_push_paused",
            "value": paused,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="key",
    ).execute()


def load_device_line_paused(device_id: str):
    """Per-device override of the global switch above. None = no override
    (follow global); True/False forces this device's push on/off. Not
    @cached, same reasoning as load_global_line_paused().

    Swallows errors and falls back to "no override" — most notably so a
    production DB that hasn't run sql/08_notify_pause_device.sql yet (no
    line_push_paused column) doesn't break rendering of this whole admin
    page section (and everything after it) with an unhandled PostgREST
    error."""
    try:
        rows = (
            sb().table("aqua_devices").select("line_push_paused").eq("device_id", device_id).execute().data
        )
        return rows[0]["line_push_paused"] if rows else None
    except Exception:
        return None


def set_device_line_paused(device_id: str, paused) -> None:
    sb().table("aqua_devices").update({"line_push_paused": paused}).eq("device_id", device_id).execute()


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
def nav_bar(current: str, on_refresh, device_id_getter=None) -> None:
    """Shared 4-item nav (首頁/異常警戒/AI預測/後台設定) rendered at the top
    of every page. Opens its own ui.header() — call it directly, not
    nested inside another header block.

    Pages are independent NiceGUI sessions with no shared state, so the
    currently-viewed device (where the target page has one) rides along
    as a ?device=... query param, same mechanism the old gear-icon link
    used for /admin. `device_id_getter` is called at CLICK time (not
    render time) so it always reflects whatever the page's device
    selector is currently set to, not just its value when the nav bar
    was first drawn."""

    def go(path: str):
        def _go():
            if device_id_getter is not None and path != "/anomalies":
                ui.navigate.to(f"{path}?device={quote(device_id_getter())}")
            else:
                ui.navigate.to(path)

        return _go

    theme = get_theme()
    if theme != DEFAULT_THEME:
        ui.query("body").classes(f"theme-{theme}")

    def pick_theme(name: str):
        def _pick():
            set_theme(name)
            ui.navigate.reload()

        return _pick

    # p-0 header: padding lives on each child instead of the header itself.
    # Gradient instead of a flat fill — just a visual refresh, same layout.
    with ui.header().classes("bg-gradient-to-r from-sky-500 to-blue-700 text-white p-0"):
        with ui.row().classes("w-full items-center justify-between flex-nowrap px-4 py-2"):
            ui.label("💧 AIoT智慧物聯系統").classes("text-lg font-semibold whitespace-nowrap")
            with ui.row().classes("items-center gap-0 shrink-0"):
                with ui.button(icon="palette").props("flat round dense color=white"):
                    with ui.menu():
                        for name, label in THEMES.items():
                            item = ui.menu_item(label, on_click=pick_theme(name))
                            if name == theme:
                                item.classes("font-bold text-sky-700")
                ui.button(icon="refresh", on_click=on_refresh).props(
                    "flat round dense color=white"
                )

    # Nav links live in two different places depending on viewport — a
    # persistent left sidebar on desktop, a bottom `ui.footer()` tab bar on
    # phones — toggled by Quasar's own breakpoint tracking rather than our
    # own CSS, since that's what burned us on `hidden md:flex` (see the
    # 2026-10-01 pitfall entry in docs/architecture.md): leaving `value`
    # unset makes QDrawer auto-show above its 1024px breakpoint and
    # auto-hide (with no toggle button, nothing to open it) below, so
    # there's no screen width with both or neither nav visible. The
    # footer's own Tailwind breakpoint is `lg:hidden` (1024px) to match.
    with ui.left_drawer().props("bordered").classes("q-pa-sm"):
        for label, path, icon in NAV_LINKS:
            is_current = path == current
            btn = ui.button(label, icon=icon, on_click=go(path)).props(
                "flat align=left no-caps"
            ).classes("w-full justify-start my-1")
            if not is_current:
                btn.classes("text-gray-600")
            elif path == "/anomalies":
                # amber, matching 異常警戒's old standalone page header — a
                # visual "this is the alert page" cue the generic sky
                # highlight below doesn't carry.
                btn.classes("bg-amber-100 text-amber-700 font-bold")
            else:
                btn.classes("bg-sky-100 text-sky-700 font-bold")

    with ui.footer().classes("bg-gray-50 text-gray-500 p-0 border-t border-gray-200 lg:hidden"):
        with ui.row().classes("w-full items-stretch justify-around flex-nowrap"):
            for label, path, icon in NAV_LINKS:
                is_current = path == current
                color = "text-sky-600" if is_current else "text-gray-500"
                with ui.column().classes(
                    f"items-center justify-center gap-0 py-1 flex-1 cursor-pointer {color}"
                ).on("click", go(path)):
                    ui.icon(icon).classes("text-2xl")
                    ui.label(label).classes("text-[11px] leading-tight font-medium")


@ui.page("/", title="💧 AIoT智慧物聯系統 · 首頁")
def main_page(device: str = "") -> None:
    ui.colors(primary="#0284c7", secondary="#0891b2", accent="#22c55e", positive="#22c55e")

    devices = load_devices()

    if not devices:
        with ui.column().classes("w-full items-center gap-3 p-16"):
            ui.icon("warning", size="xl").classes("text-amber-500")
            ui.label("尚未註冊任何裝置。").classes("text-lg text-gray-500")
        return

    ids = [d["device_id"] for d in devices]
    state = {
        "device_id": device if device in ids else ids[0],
        "range_label": "24 小時",
    }

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
        ui.label("即時資料總表").classes("text-lg font-semibold")
        # A real ui.table doesn't expose per-cell background/click easily, so
        # this fakes one with a 5-col CSS grid: each row is a `contents` div
        # (renders no box of its own, just lets its children join the grid)
        # so the whole row is one click target while every cell still lands
        # in its own grid column. 5 columns no longer fit a phone screen
        # without wrapping into an unreadable stack, so the grid gets a fixed
        # min-width and the wrapper scrolls horizontally instead of shrinking
        # columns below a legible width.
        with ui.row().classes("w-full overflow-x-auto"):
            with ui.grid(columns="1.3fr 0.9fr 0.7fr 0.7fr 1.3fr").classes(
                "gap-0 rounded overflow-hidden border border-sky-600 min-w-[640px] flex-1"
            ):
                for col in ("裝置名稱", "時間", "溫度", "濕度", "最後上線時間"):
                    ui.label(col).classes("bg-sky-600 text-white font-semibold text-sm px-3 py-2")
                for i, d in enumerate(devs):
                    did = d["device_id"]
                    lt = latest_map.get(did, {})
                    thr = load_thresholds(did)
                    reading_ts = lt.get("ts")
                    reading_time = (
                        pd.to_datetime(reading_ts, utc=True, format="ISO8601")
                        if reading_ts
                        else None
                    )
                    ls = d.get("last_seen")
                    seen = pd.to_datetime(ls, utc=True, format="ISO8601") if ls else None
                    online = bool(
                        seen and (datetime.now(timezone.utc) - seen).total_seconds() < 300
                    )
                    sel = did == state["device_id"]
                    row_bg = "bg-sky-100" if sel else ("bg-sky-50" if i % 2 else "bg-gray-50")
                    cell = f"{row_bg} px-3 py-2 text-sm border-t border-sky-100"
                    with ui.element("div").classes("contents cursor-pointer").on(
                        "click", lambda did=did: select_device(did)
                    ):
                        ui.label(f"{'🟢' if online else '🔴'} {d.get('name') or did}").classes(
                            f"{cell} font-medium"
                        )
                        ui.label(
                            reading_time.tz_convert(TZ).strftime("%m-%d %H:%M")
                            if reading_time
                            else "—"
                        ).classes(f"{cell} text-gray-600")
                        for m in ("temperature", "humidity"):
                            v = lt.get(m)
                            colour = (
                                "text-red-600" if _out_of_band(v, thr.get(m)) else "text-sky-700"
                            )
                            text = f"{v}{UNIT[m]}" if v is not None else "—"
                            ui.label(text).classes(f"{cell} font-semibold {colour}")
                        ui.label(
                            seen.tz_convert(TZ).strftime("%Y-%m-%d %H:%M:%S") if seen else "—"
                        ).classes(f"{cell} text-gray-600")

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
                # Ticks every 3 hours from midnight (00/03/06/09/12/15/18/21);
                # full date+time.
                tick_formatter = (
                    "function(value){"
                    "var m=/(\\d{2}):(\\d{2})$/.exec(value);"
                    "if(!m)return '';"
                    "var h=parseInt(m[1],10),mi=parseInt(m[2],10);"
                    "return (mi===0&&h%3===0)?value:'';"
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

    # ------------------------------------------------------------- 事件處理
    def refresh_all():
        overview_section.refresh()
        history_section.refresh()

    def on_device_change(e):
        state["device_id"] = e.value
        refresh_all()

    def on_range_change(e):
        state["range_label"] = e.value
        history_section.refresh()

    def on_refresh_click():
        clear_cache()
        refresh_all()

    # ---------------------------------------------------------------- 排版
    nav_bar("/", on_refresh_click, lambda: state["device_id"])

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            overview_section()

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("歷史趨勢").classes("text-lg font-semibold")
            dev_select = ui.select(
                ids, value=state["device_id"], label="檢視裝置", on_change=on_device_change
            ).classes("w-56")
            ui.toggle(list(RANGE_HOURS.keys()), value=state["range_label"], on_change=on_range_change)
            history_section()

    # 每 60 秒自動重新整理一次(對齊裝置上傳週期)。
    ui.timer(60.0, on_refresh_click)


# --------------------------------------------------------------------- AI 預測
@ui.page("/forecast", title="🔮 AIoT智慧物聯系統 · AI 預測")
def forecast_page(device: str = "") -> None:
    """AI 預測 used to live as a section on the main page; moved to its own
    route for the same reason 全部異常 did — a first-class nav destination
    instead of one more thing to scroll past on 首頁. Per-device (unlike
    /anomalies), so it keeps its own device selector, mirroring /admin's."""
    ui.colors(primary="#0284c7", secondary="#0891b2", accent="#22c55e", positive="#22c55e")

    devices = load_devices()
    if not devices:
        with ui.column().classes("w-full items-center gap-3 p-16"):
            ui.icon("warning", size="xl").classes("text-amber-500")
            ui.label("尚未註冊任何裝置。").classes("text-lg text-gray-500")
        return

    ids = [d["device_id"] for d in devices]
    state = {
        "device_id": device if device in ids else ids[0],
        "summary_metric": "溫度",
        "summary_model": "全部",
        "fc_metric": "溫溼度",
        # default to today (Asia/Taipei); falls back to 全部 if today has no
        # rows yet (see the date_options guard below). load_forecast_eval
        # returns the full ~7-day history (see the 2026-09-29 fetch-
        # undershoot fix), so 全部 by default would dump a week on screen.
        "fc_date": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d"),
        "fc_model": "全部",
    }

    @ui.refreshable
    def summary_section():
        """Fleet-wide, one row per (device, metric, model): the single
        latest forecast+actual pair for that combo, plus its aggregate
        平均誤差/命中率 — the same stats already computed per (metric,
        model) in forecast_section's caption loop below, just rolled up
        across every device instead of one device's own detail table.
        Device-independent by design (no 裝置 selector here), mirroring
        /anomalies's fleet-wide table."""
        wanted = METRIC_FILTERS[state["summary_metric"]]
        wanted_labels = [LABEL.get(k, k) for k in wanted]

        # Gather each device's metric-filtered eval frame once, both to
        # build the rows below and to know which models actually have
        # data (for the 模型 select's options) before applying that
        # filter.
        per_device_ev = []
        all_models: set[str] = set()
        for d in devices:
            ev = load_forecast_eval(d["device_id"])
            if ev.empty:
                continue
            ev = ev[ev["metric"].isin(wanted_labels)]
            if ev.empty:
                continue
            per_device_ev.append((d, ev))
            all_models.update(ev["model"].unique())

        model_options = ["全部"] + sorted(all_models)
        if state["summary_model"] not in model_options:
            state["summary_model"] = "全部"

        def on_summary_model_change(e):
            state["summary_model"] = e.value
            summary_section.refresh()

        with ui.row().classes("items-center gap-4"):
            ui.toggle(
                list(METRIC_FILTERS.keys()),
                value=state["summary_metric"],
                on_change=on_summary_metric_change,
            ).props("toggle-color=green")
            ui.select(
                model_options,
                value=state["summary_model"],
                label="模型",
                on_change=on_summary_model_change,
            ).classes("w-40")

        rows = []
        for d, ev in per_device_ev:
            did = d["device_id"]
            for m in METRICS:
                label = LABEL.get(m, m)
                g_metric = ev[ev["metric"] == label]
                if g_metric.empty:
                    continue
                for model_name in sorted(g_metric["model"].unique(), key=_model_sort_key):
                    if state["summary_model"] != "全部" and model_name != state["summary_model"]:
                        continue
                    # g_metric is newest-target-first already (load_forecast_eval
                    # builds it by iterating aqua_forecasts ordered ts_target
                    # desc), so each group's first row here is its latest.
                    g = g_metric[g_metric["model"] == model_name]
                    latest = g.iloc[0]
                    rows.append(
                        {
                            "device_id": d.get("name") or did,
                            "ts_target": latest["ts_target"],
                            "metric": label,
                            "model": model_name,
                            "yhat": latest["yhat"],
                            "actual": latest["actual"],
                            "err_avg": round(float(g["err"].mean()), 2),
                            "hit": latest["hit"],
                            "hit_rate": f"{(g['hit'] == '✓').mean() * 100:.0f}%",
                        }
                    )
        if not rows:
            ui.label("尚無預測資料。").classes("text-gray-500")
            return
        # Group by model first (gbm above ewma+drift) rather than by device —
        # a stable sort keeps each model group's original device/metric order.
        rows.sort(key=lambda r: _model_sort_key(r["model"]))
        cols = [
            {"name": n, "label": lb, "field": n, "align": "left", "headerClasses": "bg-green text-white"}
            for n, lb in [
                ("device_id", "裝置名稱"),
                ("ts_target", "目標時間"),
                ("metric", "項目"),
                ("model", "模型"),
                ("yhat", "預測"),
                ("actual", "實際"),
                ("err_avg", "平均誤差"),
                ("hit", "命中"),
                ("hit_rate", "命中率"),
            ]
        ]
        table_rows = [{**r, "_row_id": i} for i, r in enumerate(rows)]
        # overflow-x-auto on a plain div, not the table itself: on a phone
        # this table's 9 columns don't fit, so it needs to scroll — bounded
        # inside the card instead of bleeding past its right border with no
        # visible affordance (how it looked before this wrapper).
        with ui.element("div").classes("w-full overflow-x-auto"):
            ui.table(columns=cols, rows=table_rows, row_key="_row_id").classes("w-full")

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

        ev_all = load_forecast_eval(state["device_id"])
        if not ev_all.empty:
            ev_all = ev_all[ev_all["metric"].isin(wanted_labels)]
        if ev_all.empty:
            return

        dates = sorted(ev_all["_date"].unique(), reverse=True)
        date_options = ["全部"] + dates
        if state["fc_date"] not in date_options:
            state["fc_date"] = "全部"

        model_options = ["全部"] + sorted(ev_all["model"].unique())
        if state["fc_model"] not in model_options:
            state["fc_model"] = "全部"

        def on_fc_date_change(e):
            state["fc_date"] = e.value
            forecast_section.refresh()

        def on_fc_model_change(e):
            state["fc_model"] = e.value
            forecast_section.refresh()

        with ui.row().classes("gap-4"):
            ui.select(
                ids, value=state["device_id"], label="裝置", on_change=on_device_change
            ).classes("w-40")
            ui.select(
                date_options, value=state["fc_date"], label="日期", on_change=on_fc_date_change
            ).classes("w-40")
            ui.select(
                model_options, value=state["fc_model"], label="模型", on_change=on_fc_model_change
            ).classes("w-40")

        ev = ev_all
        if state["fc_date"] != "全部":
            ev = ev[ev["_date"] == state["fc_date"]]
        if state["fc_model"] != "全部":
            ev = ev[ev["model"] == state["fc_model"]]
        if ev.empty:
            ui.label("沒有符合篩選條件的預測資料。").classes("text-xs text-gray-400")
            return

        with ui.column().classes("gap-1 mb-2"):
            ui.label(
                "*ewma+drift模型：近期數值指數加權平均+趨勢外推，計算快、資料需求低，"
                "適合當基準模型，當環境變異大能快速抓到新趨勢。"
            ).classes("text-xs text-gray-500")
            ui.label(
                "*gbm模型：以過去14天資料訓練梯度提升樹，納入時段特徵，能學習日夜週期"
                "變化，但需足夠歷史資料才能訓練。"
            ).classes("text-xs text-gray-500")

        # headerClasses is a genuine Quasar QTable column prop (not a
        # NiceGUI one) — see the same pattern on /anomalies's table.
        cols = [
            {"name": n, "label": lb, "field": n, "align": "left", "headerClasses": "bg-green text-white"}
            for n, lb in [
                ("ts_target", "目標時間"),
                ("metric", "項目"),
                ("model", "模型"),
                ("yhat", "預測"),
                ("actual", "實際"),
                ("err", "誤差"),
                ("hit", "命中"),
            ]
        ]
        # ts_target alone can repeat across rows now (both models predict the
        # same target time each run) — row_key needs a value unique per row.
        rows = ev.reset_index(drop=True).reset_index(names="_row_id").to_dict("records")
        with ui.element("div").classes("w-full overflow-x-auto"):
            ui.table(columns=cols, rows=rows, row_key="_row_id").classes("w-full")

    def on_summary_metric_change(e):
        state["summary_metric"] = e.value
        summary_section.refresh()

    def on_device_change(e):
        state["device_id"] = e.value
        forecast_section.refresh()

    def on_fc_metric_change(e):
        state["fc_metric"] = e.value
        forecast_section.refresh()

    def on_refresh_click():
        clear_cache()
        summary_section.refresh()
        forecast_section.refresh()

    nav_bar("/forecast", on_refresh_click, lambda: state["device_id"])

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("AI 預測總表").classes("text-lg font-semibold")
            ui.label(
                "依裝置、溫/溼度、模型顯示最新一筆AI預測資料(依需求預測30分後的資料)，"
                "並與實際量測值做比較，評估誤差及命中率。"
            ).classes("text-sm text-gray-500")
            summary_section()

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("AI預測資料查詢").classes("text-lg font-semibold")
            ui.label("依裝置、日期、模型查詢AI預測資料。").classes("text-sm text-gray-500")
            ui.toggle(
                list(METRIC_FILTERS.keys()),
                value=state["fc_metric"],
                on_change=on_fc_metric_change,
            ).props("toggle-color=green")
            forecast_section()

    ui.timer(60.0, on_refresh_click)


# ------------------------------------------------------------- 全部異常(跨裝置)
@ui.page("/anomalies", title="🚨 AIoT智慧物聯系統 · 異常警戒")
def anomalies_page() -> None:
    """Fleet-wide alarm list: every device mixed into one table, sorted by
    time, independent of any single-device selection (same idea as
    Grafana/Zabbix separating Alerting from a host's own dashboard). Its
    own page/route rather than a section on the main page so it reads as
    a first-class destination, not something scrolled past — 首頁's own
    per-device 近期異常 drill-down was removed once this covered the same
    ground for every device at once."""
    ui.colors(primary="#0284c7", secondary="#0891b2", accent="#22c55e", positive="#22c55e")

    devices = load_devices()
    if not devices:
        with ui.column().classes("w-full items-center gap-3 p-16"):
            ui.icon("warning", size="xl").classes("text-amber-500")
            ui.label("尚未註冊任何裝置。").classes("text-lg text-gray-500")
        return

    dev_ids = [d["device_id"] for d in devices]
    state = {
        "metric": "溫溼度",
        # default to today (Asia/Taipei); falls back to 全部 if today has no
        # rows yet (see the date_options guard below).
        "date": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d"),
        "device": "全部",
    }

    @ui.refreshable
    def table_section():
        an = load_anomalies(None)
        wanted = METRIC_FILTERS[state["metric"]]
        if not an.empty:
            an = an[an["metric"].isin(wanted)]
        if not an.empty:
            # 一小時一筆:同一(裝置, 項目, 方法, 整點)只保留最新那筆 —
            # device_id has to be in the dedupe key here (unlike the
            # per-device table on the main page) or two devices' anomalies
            # in the same hour would collapse into one row.
            an = an.assign(_hour=an["ts"].dt.floor("h"))
            an = (
                an.sort_values("ts", ascending=False)
                .drop_duplicates(subset=["device_id", "metric", "method", "_hour"])
            )

        dates = sorted(an["ts"].dt.strftime("%Y-%m-%d").unique(), reverse=True) if not an.empty else []
        date_options = ["全部"] + dates
        if state["date"] not in date_options:
            state["date"] = "全部"
        device_options = ["全部"] + dev_ids
        if state["device"] not in device_options:
            state["device"] = "全部"

        def on_date_change(e):
            state["date"] = e.value
            table_section.refresh()

        def on_device_change(e):
            state["device"] = e.value
            table_section.refresh()

        with ui.row().classes("gap-4"):
            ui.select(
                device_options, value=state["device"], label="裝置", on_change=on_device_change
            ).classes("w-40")
            ui.select(
                date_options, value=state["date"], label="日期", on_change=on_date_change
            ).classes("w-40")
        with ui.column().classes("gap-0.5"):
            ui.label("警戒值每個整點至多一筆，警示類型分為以下兩者：").classes(
                "text-xs text-gray-400"
            )
            ui.label("*超出範圍：數值超出後台設定的上/下限。").classes(
                "text-xs text-gray-400"
            )
            ui.label(
                "*統計偏離：數值相對近1小時平均值的變化幅度異常，"
                "即使未超出上/下限也可能被標記。"
            ).classes("text-xs text-gray-400")

        if state["date"] != "全部" and not an.empty:
            an = an[an["ts"].dt.strftime("%Y-%m-%d") == state["date"]]
        if state["device"] != "全部" and not an.empty:
            an = an[an["device_id"] == state["device"]]

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
        show = an[["ts", "device_id", "metric", "value", "method", "note"]].copy()
        show["ts"] = show["ts"].dt.strftime("%m-%d %H:%M")
        show["metric"] = show["metric"].map(lambda x: LABEL.get(x, x))
        show["method"] = show["method"].map(lambda x: method_label.get(x, x))
        # headerClasses is a genuine Quasar QTable column prop (not a NiceGUI
        # one) — it lands each column's header <th> with these classes,
        # which is how a plain ui.table gets a colored header at all.
        columns = [
            {"name": n, "label": lb, "field": n, "align": "left", "headerClasses": "bg-amber-600 text-white"}
            for n, lb in [
                ("ts", "時間"),
                ("device_id", "裝置"),
                ("metric", "項目"),
                ("value", "數值"),
                ("method", "警示類型"),
                ("note", "說明"),
            ]
        ]
        rows = show.reset_index(drop=True).reset_index(names="_row_id").to_dict("records")
        with ui.element("div").classes("w-full overflow-x-auto"):
            ui.table(columns=columns, rows=rows, row_key="_row_id").classes("w-full")

    def on_metric_change(e):
        state["metric"] = e.value
        table_section.refresh()

    def on_refresh_click():
        clear_cache()
        table_section.refresh()

    nav_bar("/anomalies", on_refresh_click)

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("異常警戒").classes("text-lg font-semibold")
            ui.label(
                "依裝置、日期顯示異常警戒資訊，並發送警戒訊息至LINE群組，"
                "相同裝置警戒間隔1小時推送1筆。"
            ).classes("text-sm text-gray-500")
            ui.toggle(
                list(METRIC_FILTERS.keys()), value=state["metric"], on_change=on_metric_change
            ).props("toggle-color=orange-9")  # matches the amber nav highlight above
            table_section()

    ui.timer(60.0, on_refresh_click)


# ---------------------------------------------------------------- 管理後台
@ui.page("/admin", title="⚙️ AIoT智慧物聯系統 · 後台設定")
def admin_page(device: str = "") -> None:
    """所有需要設定/會改變裝置或雲端行為的功能都集中在這裡:推播設定、
    顯示設定(OLED 虛擬寵物)、警戒設定。主頁維持純檢視,不放任何設定。

    `device` is an optional ?device=... query param nav_bar() carries over
    from whichever page linked here, so switching to admin doesn't reset
    back to the first device in the list — pages are independent NiceGUI
    sessions with no shared state, so this query param is the only link
    between them."""
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

    OVERRIDE_LABEL = {"default": "跟隨全域設定", "pause": "強制暫停", "on": "強制開啟"}
    OVERRIDE_TO_VALUE = {"default": None, "pause": True, "on": False}

    @ui.refreshable
    def notify_section():
        paused = load_global_line_paused()

        def on_toggle(e):
            set_global_line_paused(not e.value)
            ui.notify(
                "已開啟 LINE 推播（全域）" if e.value else "已關閉 LINE 推播（全域）",
                type="positive" if e.value else "warning",
            )
            notify_section.refresh()

        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("LINE 警示推播").classes("font-medium")
                ui.label(f"全域預設：{'已關閉' if paused else '已開啟'}").classes(
                    "text-xs text-gray-400"
                )
            with ui.row().classes("items-center gap-1"):
                if paused:
                    ui.icon("notifications_off").classes("text-amber-500")
                ui.switch(value=not paused, on_change=on_toggle)

        ui.separator().classes("my-2")

        device_id = state["device_id"]
        override = load_device_line_paused(device_id)
        override_key = "default" if override is None else ("pause" if override else "on")

        def on_override_change(e, device_id=device_id):
            try:
                set_device_line_paused(device_id, OVERRIDE_TO_VALUE[e.value])
            except Exception as exc:
                ui.notify(f"更新失敗：{exc}", type="negative")
                return
            ui.notify(f"已更新「{device_id}」的個別覆寫設定。", type="positive")
            notify_section.refresh()

        ui.select(
            OVERRIDE_LABEL,
            value=override_key,
            label=f"「{device_id}」個別覆寫",
            on_change=on_override_change,
        ).classes("w-56")
        ui.label(
            "個別裝置可覆寫全域設定，例如全域開啟推播時，仍可為單一裝置（如維修中）強制暫停。"
        ).classes("text-xs text-gray-400")

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

    @ui.refreshable
    def alert_section():
        th = load_thresholds(state["device_id"])
        edits: dict = {}
        # A plain column, not a nested ui.card(): the outer SECTION_CARD_CLASSES
        # card already provides the border/shadow, and a second card here just
        # ate an extra ~32px of padding on both sides for nothing — exactly
        # the padding this row's content needed on a 390px-wide phone screen.
        with ui.column().classes("w-full gap-0"):
            for i, m in enumerate(METRICS):
                cur = th.get(m, {})
                enabled0 = bool(cur.get("enabled", False))
                row_classes = "w-full items-center gap-2 py-2 flex-nowrap"
                if i > 0:
                    row_classes += " border-t border-gray-100"
                if not enabled0:
                    row_classes += " opacity-60"
                # Every size on the number inputs needs `shrink-0`: a plain
                # ui.row() is a flexbox, and without it the browser happily
                # crushes them below their specified width to fit the row's
                # other content — on a narrow screen that silently clips the
                # digits to invisibility rather than wrapping, since a
                # <input type=number> has no line-wrap to fall back to.
                with ui.row().classes(row_classes) as row:
                    ui.icon(ICON.get(m, "info")).classes("text-xl text-sky-600 shrink-0")
                    ui.label(LABEL.get(m, m)).classes("w-16 shrink-0 text-sm font-medium")
                    mn = ui.number(value=float(cur.get("min_val") or 0.0), step=0.5).props(
                        "dense"
                    ).classes("w-14 shrink-0")
                    ui.label("–").classes("text-gray-400 shrink-0")
                    mx = ui.number(value=float(cur.get("max_val") or 0.0), step=0.5).props(
                        "dense"
                    ).classes("w-14 shrink-0")
                    if UNIT[m]:
                        ui.label(UNIT[m]).classes("text-xs text-gray-400 shrink-0 -ml-1")
                    en = ui.switch(value=enabled0).classes("ml-auto shrink-0")
                mn.set_enabled(enabled0)
                mx.set_enabled(enabled0)

                def sync_row(e, mn=mn, mx=mx, row=row):
                    mn.set_enabled(e.value)
                    mx.set_enabled(e.value)
                    if e.value:
                        row.classes(remove="opacity-60")
                    else:
                        row.classes(add="opacity-60")

                en.on_value_change(sync_row)
                edits[m] = (mn, mx, en)

            def do_save(edits=edits):
                payload = {m: (mn.value, mx.value, en.value) for m, (mn, mx, en) in edits.items()}
                save_thresholds(state["device_id"], payload)
                ui.notify("已儲存。", type="positive")
                alert_section.refresh()

            ui.button("儲存", on_click=do_save).classes("mt-2")
        ui.label(
            "雲端每分鐘檢查一次(aqua_check_thresholds),持續超標最多每小時記一筆,"
            "顯示在「異常警戒」的「超出範圍」。"
        ).classes("text-xs text-gray-400 mt-1")

    def on_device_change(e):
        state["device_id"] = e.value
        notify_section.refresh()
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

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("系統設定總覽").classes("text-lg font-semibold")
            ui.select(
                ids, value=state["device_id"], label="監測裝置", on_change=on_device_change
            ).classes("w-full")

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("警戒門檻設定").classes("text-lg font-semibold")
            alert_section()

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("推播與警示設定").classes("text-lg font-semibold")
            notify_section()

        with ui.card().classes(SECTION_CARD_CLASSES).props("flat"):
            ui.label("裝置面板顯示設定").classes("text-lg font-semibold")
            ui.label("面板可選擇不同寵物，並設定流汗、發抖門檻。").classes(
                "text-xs text-gray-400"
            )
            ui.label("「僅影響面板寵物表情，非異常警戒門檻」").classes(
                "text-xs text-gray-400 mb-1"
            )
            display_section()

    def on_refresh_click():
        # No auto ui.timer for this page — unlike the read-only pages, a
        # background refresh here would call alert_section.refresh() and
        # silently wipe whatever threshold edits are sitting unsaved in the
        # number inputs. Manual only.
        clear_cache()
        notify_section.refresh()
        display_section.refresh()
        alert_section.refresh()

    nav_bar("/admin", on_refresh_click, lambda: state["device_id"])

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-5"):
        gate_or_content()


# ---------------------------------------------------------------- 快取預熱
# Switching pages re-runs its whole render function, which on a cold cache
# makes a chain of blocking Supabase calls per device — worst offender is
# load_forecast_eval(), which pages through telemetry to match it against
# forecasts. Rather than make the user's click wait on that, a background
# loop keeps the shared @cached() entries warm continuously (server is an
# always-on Render instance, not spun up per request), so a page visit
# almost always finds the data already sitting in cache. Interval is below
# every load_*'s own TTL (30s, 60s for load_forecast_eval) so a visit is
# never more than one warm cycle away from fresh data.
CACHE_WARM_INTERVAL = 25


async def _warm_cache_once() -> None:
    devices = await run.io_bound(load_devices)
    device_ids = [d["device_id"] for d in devices]

    async def warm_device(did: str) -> None:
        await run.io_bound(load_history, did, 24)
        await run.io_bound(load_thresholds, did)
        await run.io_bound(load_forecasts, did)
        await run.io_bound(load_forecast_eval, did)

    await asyncio.gather(
        run.io_bound(load_all_latest),
        run.io_bound(load_anomalies, None),
        *(warm_device(did) for did in device_ids),
    )


async def _warm_cache_loop() -> None:
    while True:
        try:
            await _warm_cache_once()
        except Exception as exc:  # never let a bad cycle kill the loop
            print(f"[cache warm] cycle failed: {exc}", flush=True)
        await asyncio.sleep(CACHE_WARM_INTERVAL)


# Passing the async function itself (not a task/coroutine) lets NiceGUI
# wrap it with its own background_tasks.create() — same exception routing
# and clean cancellation on shutdown as every other NiceGUI background task.
app.on_startup(_warm_cache_loop)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        favicon="💧",
        reload=False,
        show=False,
        storage_secret=STORAGE_SECRET,
    )
