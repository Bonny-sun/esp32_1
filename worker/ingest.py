#!/usr/bin/env python3
"""
╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗

類別：後端（Backend）── 部署在 Render 的「背景工作程式」，沒有畫面，24 小時常駐。
目的：ESP32 把感測資料丟到 MQTT 雲端信箱，這支程式負責「收信」，共做 3 件事：
      1. 收到就整理好、存進資料庫(Supabase)
      2. 數值異常時推 LINE 通知
      3. 每 30 分鐘順便跑一次 AI 預測（analysis/baseline.py）
資料流向：ESP32 ──MQTT──> 【本程式】 ──> Supabase 資料庫 ──> 儀表板網頁 / LINE

程式架構：
  一、設定與常數          【1-1】載入 .env   【1-2】讀環境變數   【1-3】指標名稱與單位
  二、共用小工具          【2-1】取得現在時間(UTC)   【2-2】UTC 轉台北時間
  三、LINE 推播           【3-1】判斷是否暫停推播   【3-2】送出 LINE 訊息
                          【3-3】找出尚未推播的警戒並組成訊息
  四、處理一筆 MQTT 封包  【4-1】解析 JSON   【4-2】整理成資料庫欄位
                          【4-3】寫入資料庫   【4-4】檢查警戒、推 LINE
  五、MQTT 連線事件       【5-1】連上後訂閱   【5-2】斷線   【5-3】收到訊息
  六、AI 預測排程         【6-1】跑一次預測   【6-2】每 30 分鐘重複
  七、程式入口 main()     【7-1】建立 MQTT client   【7-2】連線並啟動兩條工作線

備註：
  多存一個感測欄位 ........ 【4-2】(另外還要改資料庫 sql/ 與前端)
  改 LINE 警戒訊息文字 ..... 【3-3】
  新增/改指標中文名稱 ...... 【1-3】
  改預測頻率 .............. 【6-2】的 FORECAST_INTERVAL_S
  改 MQTT 訂閱主題 ......... 【1-2】的 MQTT_TOPIC

╚════════════════════════════════════════════════════════════════════════════╝

以下為技術備註（原文保留）：

Render Background Worker — subscribe to HiveMQ Cloud (MQTT/TLS) and write
every telemetry packet into Supabase.

Why REST (supabase-py) and not psycopg2:
  the Supabase *direct* Postgres host is IPv6-only and Render has no
  outbound IPv6 (psycopg2 -> "Network is unreachable"). The PostgREST
  endpoint used here is plain HTTPS, so that whole class of problem does
  not apply. If you ever switch to psycopg2, use the IPv4 *pooler* string
  (aws-0-<region>.pooler.supabase.com), not db.<ref>.supabase.co.

Config: a local  worker/.env  is auto-loaded if present (local testing).
In production Render injects the same names as real env vars and there is
no .env file, so the loader is a harmless no-op there.

Env vars (Render dashboard -> Environment):
  MQTT_HOST              e.g. abcd1234.s1.eu.hivemq.cloud
  MQTT_PORT             8883
  MQTT_USER  MQTT_PASS
  MQTT_TOPIC           default 'aquaponics/+/+/telemetry'
  SUPABASE_URL          https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (bypasses RLS)
  LINE_CHANNEL_TOKEN     optional — LINE Messaging API channel access token.
                         Unset = LINE push disabled, everything else unchanged.
                         (LINE Notify was shut down 2025-03-31; this is the
                         Messaging API "broadcast" call, which pushes to every
                         friend of your LINE Official Account — fine for a
                         personal setup, see docs/architecture.md.)

Also runs the AI forecast job (analysis/baseline.py) on its own internal
30-min timer, in this same always-on process — see run_forecast_loop()
below. This used to be a separate GitHub Actions scheduled workflow;
GitHub's `schedule` trigger is best-effort and was silently dropping most
ticks (runs meant to be 30 min apart were landing 3-8h apart in practice).
This worker is already paid-for and already running continuously for MQTT,
so piggybacking the forecast job on it costs nothing extra and isn't at the
mercy of an external scheduler.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from supabase import create_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
import baseline  # noqa: E402

# ============================================================================
# 一、設定與常數：程式啟動時先把「連去哪、用什麼帳密」準備好
# ============================================================================

# ---- 【1-1】載入 .env（只在本機測試時有用；Render 上直接用環境變數）----
try:
    from dotenv import load_dotenv

    load_dotenv()                       # loads worker/.env if run from that dir
except ImportError:
    pass

# ---- 【1-2】讀環境變數：MQTT 連線資訊、LINE 權杖、Supabase 資料庫 ----
# 這些值設在 Render 後台 (Environment)，不寫死在程式裡，避免密碼外流。
MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ["MQTT_USER"]
MQTT_PASS = os.environ["MQTT_PASS"]
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "aquaponics/+/+/telemetry")

LINE_CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

# ---- 【1-3】指標名稱與單位：KNOWN_METRICS 有專屬欄位；LABEL/UNIT 用在 LINE 訊息 ----
# Metrics that have a dedicated column. Anything else in metrics{}/net{}
# lands in the `extra` jsonb so the firmware can add fields without a
# schema change.
KNOWN_METRICS = {"temperature", "humidity", "water_temp", "ph", "soil_moisture"}
METRIC_LABEL = {
    "temperature": "氣溫",
    "humidity": "濕度",
    "water_temp": "水溫",
    "ph": "pH",
    "soil_moisture": "土壤濕度",
}
METRIC_UNIT = {
    "temperature": "°C",
    "humidity": "%RH",
    "water_temp": "°C",
    "ph": "",
    "soil_moisture": "%",
}


# ============================================================================
# 二、共用小工具：時間處理
# ============================================================================

# ---- 【2-1】取得現在時間（UTC，字串格式），寫入資料庫時間欄位用 ----
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- 【2-2】把資料庫的 UTC 時間轉成台北時間（+8），給人看的 LINE 訊息用 ----
def _fmt_taipei(ts) -> str:
    """DB timestamps are UTC; render Asia/Taipei (+8, no DST) for a human."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# ============================================================================
# 三、LINE 推播：數值異常時通知手機（流程：3-1 先問能不能推 → 3-3 組訊息 → 3-2 送出）
# ============================================================================

# ---- 【3-1】判斷「現在要不要暫停推播」：先看該裝置的個別設定，沒設才看全域開關 ----
def line_push_paused(device_id: str) -> bool:
    """Per-device override (aqua_devices.line_push_paused) if set, else the
    global kill switch flipped from the dashboard (aqua_settings). NULL on
    the device row means "no override, follow the global default".

    The device-override check and the global check are wrapped
    separately: a failure reading the override (e.g. a production DB
    that hasn't run sql/08_notify_pause_device.sql yet, so the column
    doesn't exist) must fall back to the global check, not skip it — an
    exception here must never make the user's global on/off switch
    silently stop applying. Fails open to "not paused" only if BOTH
    checks error out, so a broken settings read still never silently
    swallows a real threshold alert."""
    try:
        dev_rows = (
            sb.table("aqua_devices").select("line_push_paused")
            .eq("device_id", device_id).execute().data
        )
        if dev_rows and dev_rows[0]["line_push_paused"] is not None:
            return bool(dev_rows[0]["line_push_paused"])
    except Exception as exc:
        print(f"[line] device override check failed (falling back to global): {exc}", flush=True)
    try:
        rows = (
            sb.table("aqua_settings").select("value")
            .eq("key", "line_push_paused").execute().data
        )
        return bool(rows and rows[0]["value"])
    except Exception as exc:
        print(f"[line] pause-check failed (assuming not paused): {exc}", flush=True)
        return False


# ---- 【3-2】送出一則 LINE 訊息（沒設權杖或被暫停就直接略過，失敗也不影響收資料）----
def push_line(text: str, device_id: str) -> None:
    """Broadcast a LINE message to every friend of the Official Account.
    No-op (silently) if LINE_CHANNEL_TOKEN isn't set or pushes are paused
    (globally, or for this device specifically)."""
    if not LINE_CHANNEL_TOKEN:
        return
    if line_push_paused(device_id):
        print("[line] paused, skipping push", flush=True)
        return
    try:
        resp = requests.post(
            LINE_BROADCAST_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"type": "text", "text": text[:4900]}]},
            timeout=10,
        )
        if resp.status_code == 200:
            print("[line] pushed", flush=True)
        else:
            print(f"[line] push failed {resp.status_code}: {resp.text[:200]}", flush=True)
    except Exception as exc:              # never let a LINE hiccup break ingest
        print(f"[line] push error: {exc}", flush=True)


# ---- 【3-3】找出「還沒通知過」的警戒紀錄，組成中文訊息後交給 3-2 送出 ----
# 警戒紀錄本身是資料庫排程(pg_cron)每分鐘自動寫入的，這裡只負責「通知」。
def notify_pending_threshold_alerts(device_id: str) -> None:
    """aqua_check_thresholds() (pg_cron, runs every minute) already writes
    one aqua_anomalies row per device/metric/hour when a value is out of its
    aqua_thresholds band. Here we CLAIM the not-yet-pushed ones with a
    single atomic UPDATE ... WHERE notified_at IS NULL (PostgREST returns
    the rows it just updated) and push those — never a separate
    select-then-update, so two overlapping calls (e.g. a Render redeploy
    briefly running two instances) can't both grab and push the same row."""
    if not LINE_CHANNEL_TOKEN:
        return
    try:
        rows = (
            sb.table("aqua_anomalies")
            .update({"notified_at": _utcnow_iso()})
            .eq("device_id", device_id).eq("method", "threshold")
            .is_("notified_at", "null")
            .execute().data
        )
    except Exception as exc:
        print(f"[line] claim failed: {exc}", flush=True)
        return
    for r in rows:
        label = METRIC_LABEL.get(r["metric"], r["metric"])
        unit = METRIC_UNIT.get(r["metric"], "")
        value, lo, hi = r.get("value"), r.get("min_val"), r.get("max_val")

        value_str = f"{value} {unit}".strip() if value is not None else "—"

        verdict = ""
        if lo is not None and hi is not None:
            if value is not None and value < lo:
                verdict = f"低於下限，正常 {lo}–{hi}"
            elif value is not None and value > hi:
                verdict = f"高於上限，正常 {lo}–{hi}"
            else:
                verdict = f"正常 {lo}–{hi}"

        push_line(
            "⚠️ AIoT系統警戒\n"
            f"時間：{_fmt_taipei(r.get('ts'))}\n"
            f"裝置：{device_id}\n"
            f"項目：{label}\n"
            f"數值：{value_str}\n"
            f"判讀：{verdict}",
            device_id,
        )


# ============================================================================
# 四、處理一筆 MQTT 封包：本程式最核心的函式（每收到一筆感測資料就跑一次）
#     流程：4-1 解析 → 4-2 整理欄位 → 4-3 寫入資料庫 → 4-4 檢查警戒推 LINE
# ============================================================================
def handle_payload(topic: str, raw: bytes) -> None:
    """一筆 MQTT 封包進來後的完整處理流程：
    【4-1】解析 JSON  【4-2】整理成資料庫欄位  【4-3】寫入裝置狀態 + 遙測資料
    【4-4】檢查有沒有尚未推播的異常警戒，推 LINE 通知。"""
    # ---- 【4-1】解析 JSON（ESP32 送來的是文字，先轉成可讀取的資料；格式壞掉就略過這筆）----
    try:
        p = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[skip] bad JSON on {topic}: {exc}", flush=True)
        return

    # ---- 【4-2】整理成資料庫欄位 ----
    # metrics = 感測數值(溫度/濕度...)；net = 網路狀態(訊號強度 rssi...)
    metrics = p.get("metrics") or {}
    net = p.get("net") or {}
    # 裝置名稱優先用封包內的，沒有就從 topic「aquaponics/站點/裝置/telemetry」第 3 段取
    parts = topic.split("/")
    device_id = p.get("device_id") or (parts[2] if len(parts) > 2 else "unknown")

    # row = 要存進「遙測歷史表」(aqua_telemetry) 的一筆資料
    row = {
        "device_id": device_id,
        "temperature": metrics.get("temperature"),
        "humidity": metrics.get("humidity"),
        "water_temp": metrics.get("water_temp"),
        "ph": metrics.get("ph"),
        "soil_moisture": metrics.get("soil_moisture"),
        "rssi": net.get("rssi"),
    }
    if p.get("ts"):                       # only when the device clock is NTP-synced
        row["ts"] = p["ts"]

    # extra = 沒有專屬欄位的其他資料，統一收進一個彈性欄位，韌體新增欄位時資料庫不用改
    extra = {k: v for k, v in metrics.items() if k not in KNOWN_METRICS}
    extra.update({k: v for k, v in net.items() if k != "rssi"})
    for k in ("schema", "fw", "uptime_s", "site_id"):
        if k in p:
            extra[k] = p[k]
    row["extra"] = extra

    # device_row = 要更新到「裝置表」(aqua_devices) 的最新狀態：最後上線時間、寵物外觀等
    device_row = {
        "device_id": device_id,
        "site_id": p.get("site_id", "default"),
        "fw_version": p.get("fw"),
        "last_seen": _utcnow_iso(),
    }
    if "pet" in p:                        # what the firmware actually has applied
        device_row["pet_skin"] = p["pet"]
    if "pet_hot" in p:
        device_row["pet_hot"] = p["pet_hot"]
    if "pet_cold" in p:
        device_row["pet_cold"] = p["pet_cold"]

    # ---- 【4-3】寫入資料庫（裝置最新狀態 + 這筆遙測紀錄）----
    # upsert = 已有這台裝置就更新、沒有就新增；insert = 每筆都新增一列歷史紀錄
    try:
        sb.table("aqua_devices").upsert(device_row, on_conflict="device_id").execute()
        sb.table("aqua_telemetry").insert(row).execute()
        print(f"[ok] {device_id} T={row['temperature']} H={row['humidity']}", flush=True)
    except Exception as exc:              # keep the worker alive on any DB hiccup
        print(f"[error] insert failed for {device_id}: {exc}", flush=True)
        return

    # ---- 【4-4】檢查是否有尚未推播的異常警戒，推送 LINE 通知（細節見 3-3）----
    notify_pending_threshold_alerts(device_id)


# ============================================================================
# 五、MQTT 連線事件：paho 套件在「連上 / 斷線 / 收到訊息」時會自動呼叫這三個函式
#     （函式名稱與參數是套件規定的，不要改）
# ============================================================================

# ---- 【5-1】連上 MQTT 後：訂閱主題（斷線重連後會再執行一次）----
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        client.subscribe(MQTT_TOPIC, qos=1)
        print(f"[mqtt] connected; subscribed to {MQTT_TOPIC}", flush=True)
    else:
        print(f"[mqtt] connect refused: rc={reason_code}", flush=True)


# ---- 【5-2】斷線時：只印一行紀錄，paho 會自動重連 ----
def on_disconnect(client, userdata, flags, reason_code, properties=None):
    print(f"[mqtt] disconnected (rc={reason_code}); paho will auto-reconnect", flush=True)


# ---- 【5-3】收到一筆訊息：交給第四項的 handle_payload() 處理 ----
def on_message(client, userdata, msg):
    handle_payload(msg.topic, msg.payload)


# ============================================================================
# 六、AI 預測排程：這支常駐程式順便當「計時器」，定時呼叫 analysis/baseline.py
# ============================================================================
FORECAST_INTERVAL_S = 1800  # 30 min — same cadence the old GitHub Actions cron aimed for


# ---- 【6-1】跑一次預測（出錯只記錄，不能影響收資料）----
def run_forecast_cycle() -> None:
    """One pass of analysis/baseline.py's job: forecast + settle due
    predictions for every registered device. Never raises — a bad cycle
    (e.g. a dropped Supabase connection) is logged and skipped, not
    allowed to take down the MQTT ingest loop running alongside it."""
    try:
        baseline.main()
    except Exception as exc:              # noqa: BLE001 - must never kill the worker
        print(f"[forecast] cycle failed: {exc}", flush=True)


# ---- 【6-2】無限迴圈：跑一次 → 睡 30 分鐘 → 再跑一次 ----
def run_forecast_loop(*, interval: float = FORECAST_INTERVAL_S, sleep=time.sleep) -> None:
    """Runs run_forecast_cycle() immediately, then every `interval` seconds,
    forever. Meant to run in the main thread while the MQTT network loop
    runs in its own background thread (client.loop_start() in main())."""
    while True:
        run_forecast_cycle()
        sleep(interval)


# ============================================================================
# 七、程式入口 main()：Render 啟動這支程式時，從這裡開始執行
#     同時有「兩條工作線」：背景線收 MQTT 資料(第五項→第四項)，
#     主線每 30 分鐘跑 AI 預測(第六項)。
# ============================================================================
def main() -> int:
    # 開機流程：建立 MQTT client -> 設定帳密/TLS -> 掛上 callback -> 連線 ->
    # 背景執行緒跑 MQTT 網路迴圈(loop_start)-> 主執行緒跑 AI 預測排程
    # (run_forecast_loop,每30分鐘一次,見上方)，往後每筆 MQTT 訊息都觸發
    # on_message -> handle_payload()。
    # ---- 【7-1】建立 MQTT client：設定帳密、TLS 加密、掛上第五項的三個事件函式 ----
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="render-ingest")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)   # validates HiveMQ's cert
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    # ---- 【7-2】連線並啟動兩條工作線：背景線收資料、主線跑預測排程 ----
    print(f"[boot] connecting to {MQTT_HOST}:{MQTT_PORT}", flush=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=45)
    client.loop_start()                   # MQTT network loop in its own thread
    print(f"[boot] forecast cycle every {FORECAST_INTERVAL_S}s, starting now", flush=True)
    run_forecast_loop()                   # blocks the main thread forever
    return 0


if __name__ == "__main__":
    sys.exit(main())
