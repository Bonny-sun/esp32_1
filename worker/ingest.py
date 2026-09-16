#!/usr/bin/env python3
"""
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
"""
from __future__ import annotations

import json
import os
import ssl
import sys
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
import requests
from supabase import create_client

try:
    from dotenv import load_dotenv

    load_dotenv()                       # loads worker/.env if run from that dir
except ImportError:
    pass

MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ["MQTT_USER"]
MQTT_PASS = os.environ["MQTT_PASS"]
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "aquaponics/+/+/telemetry")

LINE_CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_taipei(ts) -> str:
    """DB timestamps are UTC; render Asia/Taipei (+8, no DST) for a human."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def push_line(text: str) -> None:
    """Broadcast a LINE message to every friend of the Official Account.
    No-op (silently) if LINE_CHANNEL_TOKEN isn't set."""
    if not LINE_CHANNEL_TOKEN:
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
        if lo is not None and hi is not None:
            if value is not None and value < lo:
                value_str += f"（低於下限，正常 {lo}–{hi}）"
            elif value is not None and value > hi:
                value_str += f"（高於上限，正常 {lo}–{hi}）"
            else:
                value_str += f"（正常 {lo}–{hi}）"

        push_line(
            "⚠️ 魚菜共生警戒\n"
            f"裝置：{device_id}\n"
            f"項目：{label}\n"
            f"數值：{value_str}\n"
            f"時間：{_fmt_taipei(r.get('ts'))}"
        )


def handle_payload(topic: str, raw: bytes) -> None:
    try:
        p = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[skip] bad JSON on {topic}: {exc}", flush=True)
        return

    metrics = p.get("metrics") or {}
    net = p.get("net") or {}
    parts = topic.split("/")
    device_id = p.get("device_id") or (parts[2] if len(parts) > 2 else "unknown")

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

    extra = {k: v for k, v in metrics.items() if k not in KNOWN_METRICS}
    extra.update({k: v for k, v in net.items() if k != "rssi"})
    for k in ("schema", "fw", "uptime_s", "site_id"):
        if k in p:
            extra[k] = p[k]
    row["extra"] = extra

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

    try:
        sb.table("aqua_devices").upsert(device_row, on_conflict="device_id").execute()
        sb.table("aqua_telemetry").insert(row).execute()
        print(f"[ok] {device_id} T={row['temperature']} H={row['humidity']}", flush=True)
    except Exception as exc:              # keep the worker alive on any DB hiccup
        print(f"[error] insert failed for {device_id}: {exc}", flush=True)
        return

    notify_pending_threshold_alerts(device_id)


# ---- MQTT callbacks (paho v2 signatures) ------------------------------
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        client.subscribe(MQTT_TOPIC, qos=1)
        print(f"[mqtt] connected; subscribed to {MQTT_TOPIC}", flush=True)
    else:
        print(f"[mqtt] connect refused: rc={reason_code}", flush=True)


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    print(f"[mqtt] disconnected (rc={reason_code}); paho will auto-reconnect", flush=True)


def on_message(client, userdata, msg):
    handle_payload(msg.topic, msg.payload)


def main() -> int:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="render-ingest")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)   # validates HiveMQ's cert
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    print(f"[boot] connecting to {MQTT_HOST}:{MQTT_PORT}", flush=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=45)
    client.loop_forever(retry_first_connection=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
