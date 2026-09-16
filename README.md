# Smart Aquaponics Edge Monitor & AI Predictor

A job-portfolio IoT project: an ESP32 edge node with a cute pixel-pet display
streams environment telemetry over MQTT to a cloud pipeline that stores the
time-series and runs lightweight forecasting / anomaly detection.

**Phase 1 (this repo):** air temperature + humidity, OLED pet UI, RGB status
light, MQTT → Supabase, baseline ML.
**Phase 2 (planned):** water temperature (DS18B20), pH (DFRobot SEN0161-V2),
soil moisture — the data model and feature pipeline already have slots for them.

## Status — live (updated 2026-09-16)

Full pipeline deployed and running 24/7, now across **multiple boards**:

`ESP32 (×N)` → `HiveMQ Cloud` → `Render worker (aquaponics-ingest)` → `Supabase`
→ `NiceGUI dashboard ("AIoT智慧物聯系統")` + `GitHub Actions forecast job` + `LINE push`

- **Dashboard:** https://aquaponics-dashboard-bja1.onrender.com (NiceGUI on Render, Starter plan — always-on, no cold start). Sections: **裝置總覽** (multi-board summary, click a card to switch), live values (red when out of band), **AI 預測** (forecast + predicted-vs-actual hit-rate/MAE, temp/humidity/both filter), history charts (10-min bins, per-metric colours, dashed 上限/下限 threshold lines), 近期異常 (defaults to today, date + metric filters), editable alert thresholds.
- **Multi-board:** one firmware image; each board gets a unique `裝置 ID` set in the captive portal (NVS), no per-board build.
- **LINE push:** a threshold breach broadcasts to the project's LINE Official Account within ~1 min (Messaging API, not the discontinued LINE Notify — see `docs/architecture.md` §7 for setup).
- Firmware publishes one telemetry packet per minute; NTP clock + selectable
  pixel-pet skin (water-drop / fish / cat / panda) on the OLED, with its own
  hot/cold expression thresholds — both settable from the dashboard via
  retained MQTT `.../cmd`.
- Wi-Fi / MQTT credentials are provisioned at runtime via the WiFiManager
  captive portal (`AquaGuardian-Setup`), not baked into the firmware. An
  optional 2nd (backup) Wi-Fi SSID/password can also be set in the same
  portal; the firmware fails over to it via WiFiMulti if the primary AP drops.
- Supabase `pg_cron`: hourly rollup, 30-day raw retention, per-minute
  threshold alerting (dedupes to ≤1 row/device/metric/hour).
- **AI:** `.github/workflows/forecast.yml` runs `analysis/baseline.py` every
  30 min for every registered device → EWMA+drift 30-min forecast + gated
  rolling-z anomaly, written to `aqua_forecasts` / `aqua_anomalies`.

## Stack

| Layer | Tech |
|---|---|
| Edge | ESP32-WROOM-32, PlatformIO + Arduino (C++), U8g2, non-blocking `millis()` FSM |
| Transport | MQTT over TLS — HiveMQ Cloud (Serverless free) |
| Ingest | Python `paho-mqtt` worker on Render (paid plan) |
| Storage | Supabase (PostgreSQL) — wide table + `extra jsonb`, `pg_cron` rollup |
| AI | pandas + scikit-learn: EWMA+drift forecast, rolling z-score → IsolationForest |

See [`docs/architecture.md`](docs/architecture.md) for the full design, wiring
tables, JSON schema and deployment runbook.

## Repo layout

```
platformio.ini          PlatformIO project + pinned libs
include/secrets.h.example  copy -> include/secrets.h (gitignored)
src/main.cpp            firmware: sensor + pet animation + Wi-Fi/MQTT FSM
sql/schema.sql          Supabase DDL (run in the SQL editor)
worker/ingest.py        Render background worker: MQTT -> Supabase
worker/render is via    render.yaml (Blueprint) at repo root
analysis/baseline.py    pull history -> forecast + anomalies -> write back
tools/png_to_xbm.py     convert a 48x48 1-bit PNG to a C XBM array (optional)
docs/architecture.md    design doc + decisions log
```

## Quick start

### 1. Firmware
```bash
cp include/secrets.h.example include/secrets.h   # fill in Wi-Fi + HiveMQ
pio run -t upload && pio device monitor
```
Expected: OLED shows the pet + live T/H, RGB breathes green, serial prints
`[MQTT] publish OK ... /telemetry` every 15 s.

### 2. Database
Supabase SQL editor → paste `sql/schema.sql` → run. Then enable `pg_cron`
under *Database → Extensions*.

### 3. Ingest worker
Push to GitHub → Render **New + → Blueprint** → set the `sync: false` env
vars (MQTT + Supabase secrets) → deploy.

### 4. AI baseline
```bash
cd analysis
cp .env.example .env         # SUPABASE_URL + service_role key
pip install -r requirements.txt
python baseline.py           # after ~1 day of data
```

## Firmware behaviour

| Condition | Pet | RGB LED |
|---|---|---|
| 18–28 °C | blinking / breathing | green, breathing |
| > 28 °C (hysteresis 1 °C) | dizzy eyes + sweat drip | red, breathing |
| < 18 °C | shivering + chattering | blue, breathing |
| Wi-Fi down | — | amber |
| MQTT publish | — | cyan blip (200 ms) |

No `delay()` anywhere — sensor read, animation, LED breathing and MQTT each
run on their own `millis()` cadence.

## Roadmap

- [ ] Phase 2 sensors (water temp / pH / soil) — fill the reserved pins and
      the `null` metric slots; add columns to `FEATURE_COLS`
- [x] NiceGUI dashboard on Render reading Supabase
- [ ] Remote threshold config via `aquaponics/<site>/<device>/cmd`
- [ ] OTA firmware updates
- [ ] Prophet / LightGBM model once multivariate history exists
