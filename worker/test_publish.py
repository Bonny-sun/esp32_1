#!/usr/bin/env python3
"""
Send ONE fake telemetry packet to HiveMQ — verifies the full cloud path
(broker -> ingest.py -> Supabase) without needing the ESP32.

Run in a SECOND terminal while `python ingest.py` is running in the first:
    cd worker && python test_publish.py
"""
import json
import os
import ssl
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

payload = {
    "schema": 1,
    "device_id": "esp32-aqua-01",
    "site_id": "home",
    "fw": "0.1.0",
    "uptime_s": 42,
    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "metrics": {
        "temperature": 25.7,
        "humidity": 61.0,
        "water_temp": None,
        "ph": None,
        "soil_moisture": None,
    },
    "net": {"rssi": -55, "ip": "192.168.0.99"},
}

topic = "aquaponics/home/esp32-aqua-01/telemetry"

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-pub")
c.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASS"])
c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
c.connect(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "8883")), 30)
c.loop_start()

info = c.publish(topic, json.dumps(payload), qos=1)
info.wait_for_publish()
print(f"published to {topic}:\n{json.dumps(payload, indent=2)}")

time.sleep(1)
c.loop_stop()
c.disconnect()
