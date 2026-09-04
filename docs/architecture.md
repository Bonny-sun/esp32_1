# Architecture — Smart Aquaponics Edge Monitor

## 1. System overview

```
  ESP32 (WROOM-32)                HiveMQ Cloud            Render (paid)          Supabase (Postgres)
  ┌───────────────┐   MQTT/TLS    ┌────────────┐  sub    ┌─────────────┐  REST  ┌──────────────────┐
  │ DHT  OLED  RGB │ ───────────▶ │  broker    │ ──────▶ │ ingest.py   │ ─────▶ │ aqua_telemetry   │
  │ millis() FSM   │  8883 QoS0   │ pub/sub    │  QoS1   │ (worker)    │        │ aqua_devices     │
  └───────────────┘              └────────────┘         └─────────────┘        │ aqua_*_hourly    │
        ▲  publishes JSON every 60 s (Phase 1)                                  │ aqua_forecasts   │
        │                                                    ┌─────────────┐    │ aqua_anomalies   │
        └────────────────────────────────────────────────────│ baseline.py │◀───┤ (read history)   │
                              writes forecasts / anomalies    │ (cron)      │───▶│ (write results)  │
                                                              └─────────────┘    └──────────────────┘
```

The edge device never touches the database. Everything is event-driven through
MQTT, so any number of consumers (ingest, a live dashboard, an alerting rule)
can attach without changing the firmware.

## 2. Hardware

### Phase 1 wiring (in use)

| Peripheral | Signal | ESP32 pin | Notes |
|---|---|---|---|
| DHT11 (blue) | DATA | **GPIO4** | onboard pull-up; **power from 3.3V** (data line sits at VCC → 5V would over-volt the pin) |
| SSD1306 OLED 128×64 | SDA | **GPIO21** | I²C @ 400 kHz |
| | SCL | **GPIO22** | |
| RGB LED (common cathode) | R / G / B | **GPIO16 / 17 / 18** | 220 Ω per leg; driven by LEDC PWM |

Expansion-board power-rail jumper → **3.3V** (all Phase-1 parts are 3.3V).

### Phase 2 reserved pins (do not reuse)

| Future sensor | Pin | Type |
|---|---|---|
| DFRobot pH (SEN0161-V2) | GPIO36 | ADC1, input-only |
| Resistive soil moisture | GPIO39 | ADC1, input-only |
| DS18B20 waterproof water temp | GPIO19 | digital OneWire + 4.7 kΩ pull-up |

ADC1 pins are used so the ADC keeps working while Wi-Fi is on (ADC2 does not).

## 3. MQTT

### Topics

| Topic | Payload | Retained | QoS |
|---|---|---|---|
| `aquaponics/<site>/<device>/telemetry` | JSON (below) | no | 0 (device) / 1 (worker sub) |
| `aquaponics/<site>/<device>/status` | `{"online":true}` / `{"online":false}` | yes | 1 |

`status` uses the MQTT Last Will — the broker publishes `{"online":false}`
automatically if the device drops without a clean disconnect.

### Telemetry JSON (schema 1)

```json
{
  "schema": 1,
  "device_id": "esp32-aqua-01",
  "site_id": "home",
  "fw": "0.1.0",
  "uptime_s": 3600,
  "ts": "2026-09-04T02:15:00Z",
  "metrics": {
    "temperature": 26.4,
    "humidity": 58.2,
    "water_temp": null,
    "ph": null,
    "soil_moisture": null
  },
  "net": { "rssi": -58, "ip": "192.168.1.42" }
}
```

* `ts` is omitted until the device completes an NTP sync; the ingester then
  falls back to the DB `default now()`.
* Phase-2 metrics are sent as explicit `null` now. When the hardware lands,
  the firmware just fills real numbers — **no schema migration**.
* Anything in `metrics`/`net` without a dedicated DB column is folded into
  the `extra` jsonb by the worker.

## 4. Data model

| Table | Purpose |
|---|---|
| `aqua_devices` | registry, `last_seen` heartbeat |
| `aqua_telemetry` | raw time-series; wide columns + `extra jsonb` |
| `aqua_telemetry_hourly` | 1-hour rollup (avg/min/max); keeps storage bounded |
| `aqua_forecasts` | model output (`metric`, `horizon_min`, `yhat`, bounds) |
| `aqua_anomalies` | flagged points (`method` = `rolling_zscore` → `isolation_forest`) |
| `aqua_latest` (view) | newest row per device |

Design choice: **wide table + `extra jsonb`**. Known metrics are real,
indexable columns (fast for pandas / SQL / ML); `extra` is a safety valve
for unforeseen fields. Rule: once a field matters long-term, promote it to
a real column.

All `aqua_*` objects carry the `aqua_` prefix so the schema is safe to drop
into a shared Supabase project without name collisions.

RLS: enabled on every table. `anon` = read-only (dashboard). Writes come
from the `service_role` key (worker + AI job), which bypasses RLS.

Retention (`pg_cron`): rollup every 15 min, delete raw rows older than
30 days daily at 03:10 UTC.

## 5. Deployment runbook

### 5.1 HiveMQ Cloud
1. Create a **Serverless** cluster (free).
2. Add MQTT credentials (username/password) under *Access Management*.
3. Note the cluster URL → `MQTT_HOST` (port `8883`, TLS).

### 5.2 Supabase
1. New **organization** → new project inside it (the existing org is at the
   2-project free limit). Same region as Render (e.g. Singapore).
2. Write down the DB password, `Project URL`, `anon` key, `service_role` key.
3. SQL editor → paste `sql/schema.sql` → run.
4. *Database → Extensions* → enable **pg_cron** (then re-run the schedule
   lines at the bottom of `schema.sql` if needed).

### 5.3 Render worker
1. Push this repo to GitHub.
2. Render → **New + → Blueprint** → pick the repo (reads `render.yaml`).
3. Fill the `sync: false` env vars: `MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`,
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
4. Deploy. Logs should show `[mqtt] connected; subscribed to …` then
   `[ok] esp32-aqua-01 T=… H=…` once the device publishes.

### 5.4 AI job
Local: `analysis/` → `cp .env.example .env`, edit, `python baseline.py`.
Cloud: add a Render **Cron Job** (or GitHub Action) running `python baseline.py`
every 15 min once there is a day or two of data.

## 6. Free-tier watch-outs

| Risk | Mitigation |
|---|---|
| Render containers run in **UTC** (naive `datetime.now()` off by 8 h) | `TZ=Asia/Taipei` in `render.yaml`; code uses `datetime.now(timezone.utc)` explicitly; DB columns are `timestamptz` |
| Supabase **direct** connection is IPv6-only → `psycopg2 Network is unreachable` | worker uses the HTTPS PostgREST API (supabase-py), not psycopg2. If switching: use the IPv4 **pooler** string |
| Supabase pauses a project after **7 days** with zero connections | the worker holds a persistent MQTT link and writes every 15 s → never idle |
| Supabase free storage cap **500 MB** (~200 MB/device/year raw) | hourly rollup + 30-day raw retention via `pg_cron`; raise publish interval to 30–60 s if needed |
| Render Background Workers are **paid** | on the purchased plan (`plan: starter`) |

## 7. Decisions log

* **2026-09-03** — Board confirmed ESP32-WROOM-32 (not WROVER) → RGB stays on
  GPIO16/17/18. Power jumper → 3.3V.
* **2026-09-03** — Sensor: start with the DHT11 on hand; DHT22/BME280 is a
  one-line upgrade later for better data quality.
* **2026-09-03** — Broker: HiveMQ Cloud Serverless. Ingest: Python worker on
  Render (paid plan) rather than a broker→webhook bridge, to keep a
  writeable "backend service" artifact in the portfolio.
* **2026-09-04** — DB: dedicated Supabase project in a **new free org**;
  `aqua_` table prefix keeps the option of sharing open. Schema shape:
  wide + `extra jsonb`.
* **2026-09-04** — Pixel pet drawn procedurally (U8g2 primitives), not a baked
  XBM array — easier expression swaps, less flash, `tools/png_to_xbm.py` left
  as the path to hand-drawn art later.
