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
| Supabase pauses a project after **7 days** with zero connections | the worker holds a persistent MQTT link and writes every 60 s → never idle |
| Supabase free storage cap **500 MB** (~200 MB/device/year raw) | hourly rollup + 30-day raw retention via `pg_cron`; raise publish interval to 30–60 s if needed |
| Render Background Workers are **paid** | on the purchased plan (`plan: starter`) |

## 7. LINE push notifications (optional)

When a threshold breach is detected, the worker pushes a LINE message.
**LINE Notify was shut down 2025-03-31** — this uses the **Messaging API**
`broadcast` call instead, which sends to every friend of one LINE Official
Account. Fine for a single person; if you ever add other friends to the OA
they'll get the alerts too.

### Setup (one-time)

1. **Create a LINE Official Account** — free, via
   [manager.line.biz](https://manager.line.biz) → Create account. Any name.
2. **[LINE Developers Console](https://developers.line.biz/console/)** →
   your account → **Create a new provider** (if you don't have one) →
   inside it, **Create a Messaging API channel** and pick the OA from
   step 1 as its "company/organisation" — this links the two.
3. Open the new channel → **Messaging API** tab → scroll to
   **Channel access token** → **Issue** (long-lived token). Copy it.
4. On your **phone**, open the OA's page (there's a QR code on the
   Messaging API tab, or search the OA's LINE ID) and **add it as a
   friend** — broadcast only reaches friends.
5. Render → `aquaponics-ingest` → **Environment** → add
   `LINE_CHANNEL_TOKEN` = the token from step 3 → save (redeploys).

That's it — no webhook, no stored user ID. To disable, delete/blank the
env var; every code path treats a missing token as "feature off".

### How it fires

`aqua_check_thresholds()` (pg_cron, unchanged) still writes at most one
`aqua_anomalies` row per device/metric/hour when a value is outside its
`aqua_thresholds` band. The worker, once per incoming telemetry message
(~every 60 s per device), looks for rows with `method='threshold'` and
`notified_at is null`, pushes each via LINE, and stamps `notified_at`. So
the hourly log dedupe is also the LINE rate limit — no extra throttling
code, and delivery lags the actual breach by at most ~1 min. Rolling
z-score anomalies (`method='rolling_zscore'`) do **not** push — those are
informational, not real threshold breaches.

## 8. Decisions log

* **2026-09-03** — Board confirmed ESP32-WROOM-32 (not WROVER) → RGB stays on
  GPIO16/17/18. Power jumper → 3.3V.
* **2026-09-03** — Sensor: start with the DHT11 on hand; DHT22/BME280 is a
  one-line upgrade later for better data quality.
* **2026-09-03** — Broker: HiveMQ Cloud Serverless. Ingest: Python worker on
  Render (paid plan) rather than a broker→webhook bridge, to keep a
  writeable "backend service" artifact in the portfolio.
* **2026-09-04** — DB: the "new free org" trick is dead (Supabase enforces the
  2-project limit **per account** now). Reused the existing `auto-stock-picker`
  project instead; the `aqua_` table prefix keeps it isolated. Schema shape:
  wide + `extra jsonb`.
* **2026-09-04** — Pixel pet drawn procedurally (U8g2 primitives), not a baked
  XBM array — easier expression swaps, less flash, `tools/png_to_xbm.py` left
  as the path to hand-drawn art later.
* **2026-09-04** — Publish interval set to 60 s (≈1440 rows/day) — plenty for
  slow-moving environment data and lighter on the shared 500 MB.
* **2026-09-04** — Added WiFiManager captive-portal provisioning so Wi-Fi/MQTT
  creds can be changed on-site without a laptop (auto-opens on >2 min Wi-Fi
  loss, or hold BOOT 3 s after reset). `min_spiffs` partition for the extra
  flash. Setup AP carries a WPA2 password (`aquaguardian`) — open APs are
  unreliable on Android.
* **2026-09-04** — **Deployed.** Render Blueprint `ESP32_1` → `aquaponics-ingest`
  (worker, Starter) + `aquaponics-dashboard` (web, free) in **us-oregon**.
  Dashboard: https://aquaponics-dashboard-bja1.onrender.com . Cross-region to
  Supabase (ap-southeast) adds ~200 ms/query — acceptable; optional fix is
  `region: singapore` in `render.yaml`.
* **2026-09-04** — Timestamp parsing: `aqua_telemetry.ts` mixes microsecond
  (DB default `now()`) and second (device NTP) precision; dashboard + analysis
  parse with `pd.to_datetime(..., format="ISO8601")`.
* **2026-09-07** — Dashboard rewritten on **NiceGUI** (from Streamlit) for a
  more polished UI; merged via PR #1. Firmware gained backup Wi-Fi (2nd SSID +
  WiFiMulti), zh-TW captive-portal strings, and selectable OLED pet skins
  (drop/fish/cat/panda) settable in the portal or remotely via retained MQTT
  `.../cmd`. `aqua_devices.pet_skin` column (`sql/03_pet_skin.sql`).
* **2026-09-07** — Fixed `saveParamsCallback()` restarting before WiFiManager
  applied the new SSID/pass (rebooted onto old creds); now a deferred restart
  from `loop()` once connected or after a 15 s grace.
* **2026-09-07** — **PostgREST 1000-row cap**: `load_history` (dashboard *and*
  `baseline.py`) ordered ascending + `.limit()`, so it only ever got the
  OLDEST ~1000 rows of the window — chart ended ~7 h early and the first AI
  run scored on 3-day-old data. Fix: page NEWEST-first
  (`.order(col, desc=True).range(p*1000, p*1000+999)`) until a short page.
* **2026-09-07** — Chart timezone: ECharts axis TZ handling is unreliable, and
  **pandas 2.x drops the tz on a tz-aware `.resample()`** (Render runs 2.2.x;
  local dev 3.0.5 does not — bug only showed in prod). `load_history` now
  returns a tz-naive index already holding Asia/Taipei wall-clock
  (`… .tz_localize(None) + Timedelta(hours=8)`); category axis, `strftime`
  labels; raw data binned to 10-min means; per-metric line colours.
* **2026-09-07** — Dashboard **AI 預測** section (latest forecast + predicted-
  vs-actual table + hit-rate/MAE). `.github/workflows/forecast.yml` runs
  `baseline.py` every 30 min (`workflow_dispatch` also); needs repo secrets
  `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.
* **2026-09-07** — z-score anomaly detector was crying wolf on near-flat
  signals (tiny rolling std → huge z from a trivial wiggle). Now needs z>3.5
  **and** a per-metric minimum absolute deviation, with a std floor.
* **2026-09-09** — Anomalies: `aqua_check_thresholds()` dedupe window 10 min
  → 1 h; dashboard 近期異常 gains a 溫度/濕度/溫溼度 filter + hourly thinning.
  Same filter added to the AI 預測 section (`METRIC_FILTERS`).
* **2026-09-09** — Live metric cards turn **red** when the value is outside
  that device's enabled `aqua_thresholds` band (`_out_of_band()` helper).
* **2026-09-09** — **Multi-board.** One firmware image → per-board `裝置 ID`
  field in the captive portal (NVS key `devid`), used for the MQTT client id,
  topic paths and payload. Dashboard: a **裝置總覽** row (shows when ≥2
  devices) with each board's live temp/humidity + online dot; click a card to
  switch the detail view.
* **2026-09-09** — Redrew the cat (pointed ears + white whiskers) and panda
  (white-rimmed black ears + tilted black eye patches) — the old versions
  were near-identical blobs on the 48 px OLED.
* **2026-09-09** — Pet-expression thresholds are runtime now: firmware
  `g_tempHot`/`g_tempCold` (NVS `thi`/`tlo`, default 28/18), set from the
  dashboard 虛擬寵物 card, delivered as one retained MQTT
  `{"pet":…,"pet_hot":…,"pet_cold":…}` on `.../cmd`, echoed back in telemetry.
  `sql/04_pet_temp.sql` adds `aqua_devices.pet_hot` / `pet_cold`. Hysteresis
  stays fixed at 1 °C. Independent of the cloud `aqua_thresholds` alerting.
* **2026-09-16** — LINE push on threshold breach. Chose the Messaging API
  `broadcast` call (not per-user push) to avoid needing a webhook receiver
  for the user ID — appropriate since it's one person's OA. Put the trigger
  in the **worker**, not a `pg_net`/Vault call inside `aqua_check_thresholds()`
  — keeps the whole feature in Python/Render logs the user already knows,
  at the cost of ~1 min extra latency (next telemetry message, not
  immediate). `aqua_anomalies.notified_at` (`sql/05_notify.sql`) is both the
  "already pushed" flag and, combined with the existing hourly dedupe,
  the notification rate limit.
* **2026-09-16** — Two LINE bugs fixed same day: (1) turning the feature on
  broadcast the entire historical backlog at once (every pre-existing
  `notified_at is null` row) — one-time backfill in `sql/05_notify.sql`
  marks old rows notified before the column is used. (2) A Render redeploy
  briefly running two worker instances let both claim-and-push the same
  row — `notify_pending_threshold_alerts()` now does one atomic
  `UPDATE ... WHERE notified_at IS NULL` (PostgREST returns the claimed
  rows) instead of select-then-update, so a row can only ever be claimed
  once. Also: the push text showed raw UTC (`_fmt_taipei()` now converts)
  and duplicated the band info as English `note` text — redesigned to
  labelled fields with a separate 判讀 (verdict) line, and
  `aqua_anomalies.min_val`/`max_val` (`sql/06_anomaly_bounds.sql`,
  populated by `aqua_check_thresholds()`) replaced parsing bounds out of
  `note`.
* **2026-09-16** — Dashboard/LINE rebrand to **AIoT智慧物聯系統** (matches
  the LINE Official Account name) — page title, header bar, push header.
* **2026-09-16** — Mobile-session additions merged to main: `baseline.py`
  processes every device in `aqua_devices` (not a hardcoded ID) and dedupes
  anomaly inserts against what's already stored so a 30-min cron doesn't
  re-write the same z-score point on every run; dashboard 近期異常 gained a
  日期 dropdown (options = dates actually present) — defaulted to **今天**
  instead of 全部 so it opens un-scrolled.
* **2026-09-16** — Chart: overlay the 警戒範圍 bounds as dashed ECharts
  `markLine`s. Two follow-on bugs from this: `yAxis scale:true` only sizes
  to the *series* data, so a bound far from the current reading (e.g.
  humidity running 67-72 against a 40-60 band) fell outside the visible
  range — now compute explicit `min`/`max` from data union bounds (+8% pad)
  whenever a markLine is present. And the line labels were clipped at the
  right edge (`grid.right` too small + default label position past the
  line's end) — moved to `insideEndTop` and widened the margin.
* **2026-09-16** — X-axis tick labels: index-based `interval:'auto'`
  thinning drifted off clean clock times (11:40, 12:40, ... since the 24h
  window rarely starts on a boundary). Switched to a client-side JS
  `axisLabel` formatter — NiceGUI evaluates any option key prefixed `:` as
  JavaScript (`convertDynamicProperties` in `dynamic_properties.js`,
  confirmed in the installed package, also how `EChart.from_pyecharts`
  handles `JsCode`) — that blanks any tick not exactly on a 3-hour
  boundary. First deliberate use of that mechanism in this codebase.
* **2026-09-21** — README rewritten for portfolio use (Mermaid architecture,
  dashboard screenshot + hardware photo in `docs/images/`, wiring table).
  Added `tests/` (pytest, Supabase faked via `tests/conftest.py`), `ruff`
  config in `pyproject.toml`, and `.github/workflows/ci.yml` (ruff + pytest +
  `pio run`, CI badge in README). CI green on `6c40ddc`.
  **OPEN BUG (handoff — not yet fixed):** `analysis/baseline.py`
  `detect_univariate` (rolling z-score, method `rolling_zscore`, shown as
  「統計偏離」) can never fire. The 12-point rolling window includes the point
  being scored, so |z| <= (n-1)/sqrt(n) = 3.18 < `Z_THRESH` 3.5 (verified
  empirically, max 3.175 even for huge spikes). Threshold alerts / LINE push
  are unaffected (they use `aqua_check_thresholds()` in SQL, method
  `threshold`). Pinned by a strict `xfail` in
  `tests/test_baseline.py::test_zscore_flags_a_real_spike`.
  **Planned fix:** in `build_features`/`detect_univariate`, score each point
  against the PREVIOUS window (`roll_mean`/`roll_std` computed on
  `out[c].shift(1)`), keep the `Z_MIN_ABS_DEV` / `Z_STD_FLOOR` gates, remove
  the xfail. **Before pushing:** dry-run against real data (read-only, no
  `save()`) and count how many new `rolling_zscore` anomalies appear per day
  for each device; if it is more than a handful per day, raise the gates
  first. Once fixed the dashboard 近期異常 will start showing 「統計偏離」rows
  (expected, intended) — the next `forecast` workflow run (every 30 min)
  writes them. Note `test_zscore_ignores_tiny_wiggle_on_flat_signal` currently
  passes only because nothing fires; re-check it after the fix.
* **2026-09-21 (later) — FIXED.** `build_features` now also computes
  `{col}_roll_mean_prev`/`{col}_roll_std_prev` — rolling stats over
  `out[c].shift(1)`, i.e. the window ending right before the point being
  scored, never including it. `detect_univariate` scores against these
  `_prev` columns instead of the inclusive `_roll_mean`/`_roll_std` (which
  `forecast()` still uses unchanged — that's a "current volatility" estimate,
  not a self-comparison, so it was never part of the bug). Removed the
  `xfail` from `test_zscore_flags_a_real_spike`; it and
  `test_zscore_ignores_tiny_wiggle_on_flat_signal` both pass for real now
  (32/32 tests, ruff clean). **Still open:** the real-data dry-run called for
  above — nobody has run `baseline.py` against production Supabase since
  this fix to confirm daily `rolling_zscore` volume is sane before the next
  scheduled `forecast` workflow run writes rows with it live. Do that first;
  raise `Z_THRESH`/the `Z_MIN_ABS_DEV` gates if it's noisy.
* **2026-09-22 — champion-challenger forecasting.** `ewma+drift` is a local
  linear extrapolation — structurally unable to anticipate a predictable
  diurnal swing (e.g. evening cool-down), since it only ever looks at the
  recent slope. Added `forecast_gbm()`: a `GradientBoostingRegressor`
  (scikit-learn, already a dependency) retrained from scratch every run on
  the same 72h window, using the existing lag/rolling `_prev` features plus
  a new `hour_sin`/`hour_cos` time-of-day feature (a raw hour number looks
  like a cliff to a tree split; sin/cos makes 23:00 and 00:00 neighbours).
  Direct multi-step supervised forecasting: each training row's target is
  `out[col]` shifted back `HORIZON_STEPS`, i.e. a value that — for every row
  except the very last one — already happened; only the final row's target
  is unknown, and that's the one row actually predicted. Confidence band
  comes from a holdout split's residual std, not in-sample error (which
  would understate it), then the model is refit on all of `train` for the
  actual point forecast. Returns `None` below `GBM_MIN_TRAIN_ROWS=30` rather
  than fit noise.
  `ewma+drift` is untouched and keeps running — both write to
  `aqua_forecasts` every cycle, distinguished only by the `model` column, so
  nothing is replaced and a `gbm` bug can't take down the existing forecast.
  Dashboard's 「上次預測 vs 實際」now groups hit-rate/MAE by (metric, model)
  instead of metric alone, so the two can be compared head-to-head once
  enough evaluated pairs accumulate (see the "how long to observe" note:
  want 1-2+ days, ideally spanning a real day/night transition, before
  trusting either model's numbers over the other's).
  Tests: `forecast_gbm` returns `None` on too little data, produces a sane
  in-range prediction on a synthetic diurnal signal, is deterministic
  (`random_state=42`), and a training-frame test confirms the row being
  predicted never appears with a real target (its target doesn't exist
  yet). `test_process_device_saves_both_forecast_models` confirms both
  `ewma+drift` and `gbm` rows actually get written per run. 37/37 tests
  pass, ruff clean.
