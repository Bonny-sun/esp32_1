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
"""
from __future__ import annotations

import json
import os
import ssl
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
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

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

# Metrics that have a dedicated column. Anything else in metrics{}/net{}
# lands in the `extra` jsonb so the firmware can add fields without a
# schema change.
KNOWN_METRICS = {"temperature", "humidity", "water_temp", "ph", "soil_moisture"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    try:
        sb.table("aqua_devices").upsert(
            {
                "device_id": device_id,
                "site_id": p.get("site_id", "default"),
                "fw_version": p.get("fw"),
                "last_seen": _utcnow_iso(),
            },
            on_conflict="device_id",
        ).execute()
        sb.table("aqua_telemetry").insert(row).execute()
        print(f"[ok] {device_id} T={row['temperature']} H={row['humidity']}", flush=True)
    except Exception as exc:              # keep the worker alive on any DB hiccup
        print(f"[error] insert failed for {device_id}: {exc}", flush=True)


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
