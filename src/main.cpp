// ============================================================================
//  Smart Aquaponics Edge Monitor — Phase 1 (MVP)
//  ESP32-WROOM-32 | DHT (temp/humidity) | SSD1306 128x64 OLED | common-cathode RGB
//
//  Design rules:
//   * NO delay() anywhere. Everything is millis()-based, cooperative FSM.
//   * Wi-Fi + MQTT auto-reconnect, non-blocking (only the TLS/MQTT connect
//     attempt can briefly stall; it is throttled to once / 3 s).
//   * OLED: left 48x48 = pixel "water guardian" pet with mood animation,
//           right = Wi-Fi/MQTT icons + live temp/humidity + status string.
//   * RGB LED: breathing green = comfy, red = hot, blue = cold,
//              cyan blip = MQTT publish, amber = offline.
//   * MQTT JSON telemetry is forward-compatible: metrics.{water_temp,ph,
//     soil_moisture} are published as null now; hardware upgrade only fills them.
//   * Provisioning (WiFiManager): first boot / no saved Wi-Fi, OR Wi-Fi down
//     >2 min (e.g. after a house move) -> starts AP "AquaGuardian-Setup" with
//     a captive portal for Wi-Fi + MQTT creds. To force it manually: reset the
//     board, then hold BOOT (GPIO0) within the first 3 s. secrets.h values are
//     only compile-time DEFAULTS; the portal overrides them into NVS.
//
//  Pixel pet note: the sprite is drawn procedurally (U8g2 primitives) instead of
//  a baked XBM byte-array. Reasons: trivial to swap facial expressions, no
//  288-byte/frame hex to maintain, less flash. To use hand-drawn art instead,
//  run tools/png_to_xbm.py on a 48x48 1-bit PNG and drawXBM() the result here.
// ============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>          // captive-portal provisioning (tzapu)
#include <Preferences.h>          // NVS storage for runtime MQTT config
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <U8g2lib.h>
#include <DHT.h>
#include <time.h>
#include <math.h>

// ---- Secrets --------------------------------------------------------------
#if __has_include("secrets.h")
  #include "secrets.h"
#else
  #warning "include/secrets.h not found — using placeholders. Copy secrets.h.example."
  #define WIFI_SSID   "your-ssid"
  #define WIFI_PASS   "your-pass"
  #define MQTT_HOST   "xxxx.s1.eu.hivemq.cloud"
  #define MQTT_PORT   8883
  #define MQTT_USER   "esp32"
  #define MQTT_PASS   "your-pass"
  #define SITE_ID     "home"
  #define DEVICE_ID   "esp32-aqua-01"
  #define FW_VERSION  "0.1.0"
#endif

// ============================ Configuration ===============================

// --- Pin map (Phase 1) ---------------------------------------------------
static const int PIN_DHT   = 4;    // DHT data
static const int PIN_SDA   = 21;   // OLED I2C SDA
static const int PIN_SCL   = 22;   // OLED I2C SCL
static const int PIN_LED_R = 16;   // RGB LED - red   (220 ohm)
static const int PIN_LED_G = 17;   // RGB LED - green (220 ohm)
static const int PIN_LED_B = 18;   // RGB LED - blue  (220 ohm)
// --- Reserved for Phase 2 (do NOT reuse) --------------------------------
//   GPIO36 (ADC1, input-only) -> pH analog
//   GPIO39 (ADC1, input-only) -> soil moisture analog
//   GPIO19 (digital OneWire)  -> DS18B20 waterproof temp  (+4.7k pull-up)

// --- DHT type: change this ONE line for a DHT22/AM2302 upgrade ----------
#define DHT_TYPE DHT11             // DHT11  (blue module you have now)
// #define DHT_TYPE DHT22          // DHT22 / AM2302 (white module, better data)

// --- Thresholds with hysteresis (degrees C) ---------------------------
static const float TEMP_HOT  = 28.0f;   // -> HOT mood above this
static const float TEMP_COLD = 18.0f;   // -> COLD mood below this
static const float TEMP_HYST = 1.0f;    // must recover by this much to return COMFY

// --- Timing (ms) -------------------------------------------------------
static const uint32_t SENSOR_MS   = 2000;    // DHT11 max sample rate ~1 Hz
static const uint32_t DISPLAY_MS  = 100;     // ~10 fps redraw (blink/shiver)
static const uint32_t LED_MS      = 20;      // breathing LED update
static const uint32_t PUBLISH_MS  = 60000;   // MQTT telemetry interval (1 min -> ~1440 rows/day)
static const uint32_t WIFI_RETRY_MS = 5000;
static const uint32_t MQTT_RETRY_MS = 3000;

// --- LEDC (PWM) channels for RGB -------------------------------------
static const int CH_R = 0, CH_G = 1, CH_B = 2;
static const int LED_FREQ = 5000, LED_RES = 8;   // 8-bit: 0..255

// --- Provisioning ---------------------------------------------------
static const char*    AP_NAME          = "AquaGuardian-Setup";
static const char*    AP_PASSWORD      = "aquaguardian";  // >= 8 chars; WPA2 on the setup AP (open APs are flaky on Android)
static const int      PIN_CONFIG_BTN   = 0;      // BOOT button — hold 3 s AFTER power-on to force the portal
static const uint16_t PORTAL_TIMEOUT_S = 180;    // portal auto-closes after this idle time

// ============================ Globals ====================================

U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, PIN_SCL, PIN_SDA);
DHT dht(PIN_DHT, DHT_TYPE);

WiFiClientSecure netClient;
PubSubClient     mqtt(netClient);
WiFiManager      wm;
Preferences      prefs;

// Runtime MQTT config — loaded from NVS, falling back to secrets.h defaults.
String   g_mqttHost = MQTT_HOST;
uint16_t g_mqttPort = MQTT_PORT;
String   g_mqttUser = MQTT_USER;
String   g_mqttPass = MQTT_PASS;

// Captive-portal custom fields
WiFiManagerParameter p_host("host", "MQTT host", "", 64);
WiFiManagerParameter p_port("port", "MQTT port", "", 6);
WiFiManagerParameter p_user("user", "MQTT username", "", 32);
WiFiManagerParameter p_pass("pass", "MQTT password", "", 64);

char TOPIC_TELEMETRY[80];
char TOPIC_STATUS[80];

enum Mood { COMFY, HOT, COLD };
Mood  g_mood = COMFY;
float g_temp = NAN;
float g_hum  = NAN;
bool  g_timeSynced = false;
uint32_t g_lastPublishFlash = 0;   // millis() of last MQTT publish (for LED blip)
uint32_t g_bootMs = 0;

// ============================ Helpers ====================================

static inline float round1(float v) { return roundf(v * 10.0f) / 10.0f; }

void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  // common cathode -> higher duty = brighter
  ledcWrite(CH_R, r);
  ledcWrite(CH_G, g);
  ledcWrite(CH_B, b);
}

// ISO-8601 UTC timestamp, or "" if NTP not synced yet (worker fills server time)
String isoTimestamp() {
  time_t now = time(nullptr);
  if (now < 1700000000) return String();          // clock not set
  struct tm t;
  gmtime_r(&now, &t);                              // always UTC, ignores the TZ set below
  char buf[25];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &t);
  return String(buf);
}

// Local wall-clock "HH:MM" for the OLED (Asia/Taipei, UTC+8, no DST),
// or "--:--" until NTP has synced.
void localHHMM(char *buf, size_t n) {
  time_t now = time(nullptr);
  if (now < 1700000000) { snprintf(buf, n, "--:--"); return; }
  struct tm t;
  localtime_r(&now, &t);
  strftime(buf, n, "%H:%M", &t);
}

// ---- Runtime MQTT config (NVS <- portal, defaults <- secrets.h) --------
void loadMqttConfig() {
  prefs.begin("aqua", true);                       // read-only
  g_mqttHost = prefs.getString("host", MQTT_HOST);
  g_mqttPort = prefs.getUShort("port", MQTT_PORT);
  g_mqttUser = prefs.getString("user", MQTT_USER);
  g_mqttPass = prefs.getString("pass", MQTT_PASS);
  prefs.end();
}

// WiFiManager calls this after the user saves the portal form.
void saveParamsCallback() {
  prefs.begin("aqua", false);
  if (strlen(p_host.getValue())) prefs.putString("host", p_host.getValue());
  if (strlen(p_port.getValue())) prefs.putUShort("port", (uint16_t)atoi(p_port.getValue()));
  if (strlen(p_user.getValue())) prefs.putString("user", p_user.getValue());
  if (strlen(p_pass.getValue())) prefs.putString("pass", p_pass.getValue());
  prefs.end();
  Serial.println("[CFG] saved — restarting");
  delay(300);
  ESP.restart();                                   // clean restart with the new config
}

void updateMood(float t) {
  if (isnan(t)) return;
  switch (g_mood) {
    case COMFY:
      if      (t > TEMP_HOT)  g_mood = HOT;
      else if (t < TEMP_COLD) g_mood = COLD;
      break;
    case HOT:
      if (t < TEMP_HOT - TEMP_HYST)  g_mood = COMFY;
      break;
    case COLD:
      if (t > TEMP_COLD + TEMP_HYST) g_mood = COMFY;
      break;
  }
}

// ============================ Pixel pet (left 48x48) =====================

void drawPet() {
  const bool blink = (millis() % 4000) < 150;
  int dx = 0;
  if (g_mood == COLD) dx = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter
  const int cx = 23 + dx;

  // --- teardrop body: pointed top + round bottom (filled) ---
  u8g2.setDrawColor(1);
  u8g2.drawTriangle(cx, 6, cx - 11, 30, cx + 11, 30);
  u8g2.drawDisc(cx, 34, 14);

  // --- face features punched in black on the white body ---
  u8g2.setDrawColor(0);

  // eyes
  if (g_mood == HOT) {                         // dizzy X eyes
    u8g2.drawLine(cx - 9, 29, cx - 4, 34); u8g2.drawLine(cx - 9, 34, cx - 4, 29);
    u8g2.drawLine(cx + 4, 29, cx + 9, 34); u8g2.drawLine(cx + 4, 34, cx + 9, 29);
  } else if (g_mood == COLD) {                 // squinting  u_u
    u8g2.drawLine(cx - 9, 31, cx - 6, 34); u8g2.drawLine(cx - 6, 34, cx - 3, 31);
    u8g2.drawLine(cx + 3, 31, cx + 6, 34); u8g2.drawLine(cx + 6, 34, cx + 9, 31);
  } else if (blink) {                          // COMFY blink
    u8g2.drawHLine(cx - 9, 32, 6);
    u8g2.drawHLine(cx + 3, 32, 6);
  } else {                                     // COMFY open eyes
    u8g2.drawDisc(cx - 6, 32, 2);
    u8g2.drawDisc(cx + 6, 32, 2);
  }

  // mouth
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, 40, 3);                  // panting open mouth
  } else if (g_mood == COLD) {
    u8g2.drawBox(cx - 4, 39, 8, 4);            // chattering mouth block
    u8g2.setDrawColor(1);                      // white "teeth" lines
    u8g2.drawVLine(cx - 2, 39, 4);
    u8g2.drawVLine(cx,     39, 4);
    u8g2.drawVLine(cx + 2, 39, 4);
    u8g2.setDrawColor(0);
  } else {
    u8g2.drawLine(cx - 4, 40, cx, 43);        // gentle smile
    u8g2.drawLine(cx, 43, cx + 4, 40);
  }

  // --- extras outside the body (white) ---
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {
    int sy = 8 + (int)((millis() / 150) % 10);        // dripping sweat
    u8g2.drawDisc(cx + 13, sy, 2);
    u8g2.drawLine(cx - 14, 4, cx - 11, 2);            // heat wiggle
    u8g2.drawLine(cx - 11, 2, cx - 8, 4);
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, 24, 6, 22);  u8g2.drawLine(2, 28, 6, 26);   // shiver marks
    u8g2.drawLine(40, 24, 44, 26); u8g2.drawLine(40, 28, 44, 30);
  }
}

// ============================ Stats panel (right) ========================

void drawStats() {
  const int x = 51;

  // Wi-Fi signal bars (0..3)
  int lvl = 0;
  if (WiFi.status() == WL_CONNECTED) {
    long r = WiFi.RSSI();
    lvl = (r > -60) ? 3 : (r > -70) ? 2 : 1;
  }
  for (int i = 0; i < 3; i++) {
    int h = 3 + i * 3;
    if (i < lvl) u8g2.drawBox(x + i * 4, 13 - h, 3, h);
    else         u8g2.drawFrame(x + i * 4, 13 - h, 3, h);
  }

  // MQTT indicator dot (no label — room reserved for the clock)
  if (mqtt.connected()) u8g2.drawDisc(x + 15, 9, 3);
  else                  u8g2.drawCircle(x + 15, 9, 3);

  // clock HH:MM (local), right-aligned with a 2 px margin
  char clk[8];
  localHHMM(clk, sizeof(clk));
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(126 - u8g2.getStrWidth(clk), 13, clk);

  // temperature / humidity
  char buf[24];
  u8g2.setFont(u8g2_font_7x14B_tr);
  if (!isnan(g_temp)) snprintf(buf, sizeof(buf), "%.1f C", g_temp);
  else                strcpy(buf, "--.- C");
  u8g2.drawStr(x, 30, buf);

  if (!isnan(g_hum)) snprintf(buf, sizeof(buf), "%.0f %%RH", g_hum);
  else               strcpy(buf, "-- %RH");
  u8g2.drawStr(x, 46, buf);

  // status line
  u8g2.setFont(u8g2_font_5x8_tr);
  const char* s = (g_mood == HOT)  ? "Status: Hot!"
                : (g_mood == COLD) ? "Status: Cold!"
                                   : "Status: Comfy";
  u8g2.drawStr(x, 62, s);
}

// ============================ FSM ticks ==================================

void tickSensor() {
  static uint32_t last = 0;
  if (millis() - last < SENSOR_MS) return;
  last = millis();

  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (!isnan(t)) g_temp = t;         // keep last good reading on NaN
  if (!isnan(h)) g_hum  = h;
  updateMood(g_temp);
}

void drawSetupScreen() {
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(0, 11, "SETUP MODE");
  u8g2.setFont(u8g2_font_5x8_tr);
  u8g2.drawStr(0, 23, "join Wi-Fi:");
  u8g2.drawStr(0, 33, AP_NAME);
  u8g2.drawStr(0, 43, "pass: ");
  u8g2.drawStr(30, 43, AP_PASSWORD);
  u8g2.drawStr(0, 53, "open 192.168.4.1");
}

void tickDisplay() {
  static uint32_t last = 0;
  if (millis() - last < DISPLAY_MS) return;
  last = millis();

  if (wm.getConfigPortalActive()) {
    drawSetupScreen();
    u8g2.sendBuffer();
    return;
  }

  u8g2.clearBuffer();
  drawPet();
  drawStats();
  u8g2.sendBuffer();
}

void tickLed() {
  static uint32_t last = 0;
  if (millis() - last < LED_MS) return;
  last = millis();

  // breathing envelope 0..1 over 3 s
  float phase  = (millis() % 3000) / 3000.0f;
  float breath = (sinf(phase * 2.0f * PI - PI / 2.0f) + 1.0f) * 0.5f;
  uint8_t lvl  = 15 + (uint8_t)(breath * 220);

  if (millis() - g_lastPublishFlash < 200) { setRGB(0, 160, 160); return; }  // publish blip
  if (WiFi.status() != WL_CONNECTED)       { setRGB(lvl, lvl / 4, 0); return; } // amber offline

  switch (g_mood) {
    case COMFY: setRGB(0, lvl, 0); break;
    case HOT:   setRGB(lvl, 0, 0); break;
    case COLD:  setRGB(0, 0, lvl); break;
  }
}

void publishTelemetry() {
  JsonDocument doc;
  doc["schema"]    = 1;
  doc["device_id"] = DEVICE_ID;
  doc["site_id"]   = SITE_ID;
  doc["fw"]        = FW_VERSION;
  doc["uptime_s"]  = (millis() - g_bootMs) / 1000;

  String ts = isoTimestamp();
  if (ts.length()) doc["ts"] = ts;          // omitted until NTP sync -> worker uses server time

  JsonObject m = doc["metrics"].to<JsonObject>();
  if (isnan(g_temp)) m["temperature"] = nullptr; else m["temperature"] = round1(g_temp);
  if (isnan(g_hum))  m["humidity"]    = nullptr; else m["humidity"]    = round1(g_hum);
  m["water_temp"]    = nullptr;             // Phase 2 — zero-migration placeholder
  m["ph"]            = nullptr;             // Phase 2
  m["soil_moisture"] = nullptr;             // Phase 2

  JsonObject net = doc["net"].to<JsonObject>();
  net["rssi"] = WiFi.RSSI();
  net["ip"]   = WiFi.localIP().toString();

  char payload[384];
  size_t n = serializeJson(doc, payload, sizeof(payload));
  bool ok = mqtt.publish(TOPIC_TELEMETRY, (const uint8_t*)payload, n, false);
  Serial.printf("[MQTT] publish %s (%u B) -> %s\n", ok ? "OK" : "FAIL", (unsigned)n, TOPIC_TELEMETRY);
  g_lastPublishFlash = millis();
}

void tickPublish() {
  static uint32_t last = 0;
  if (millis() - last < PUBLISH_MS) return;
  if (!mqtt.connected()) return;
  last = millis();
  publishTelemetry();
}

// ---- connectivity FSM (non-blocking) ----------------------------------

void tickWiFi() {
  static uint32_t downSince = 0;
  wm.process();                          // services the captive portal when active; no-op otherwise

  if (WiFi.status() == WL_CONNECTED) {
    downSince = 0;
    if (!g_timeSynced) {
      configTzTime("CST-8", "pool.ntp.org", "time.nist.gov");  // local = UTC+8 (Taiwan, no DST); MQTT ts stays UTC via gmtime_r
      g_timeSynced = true;                                     // NTP fills in async
    }
    return;
  }
  // Not connected: WiFiManager + the ESP32 core auto-reconnect to the saved AP.
  // If Wi-Fi stays down for a long stretch, reopen the portal so creds can be
  // fixed on-site without a laptop.
  if (downSince == 0) downSince = millis();
  if (!wm.getConfigPortalActive() && millis() - downSince > 120000UL) {
    Serial.println("[WiFi] down >2 min — opening config portal");
    wm.startConfigPortal(AP_NAME, AP_PASSWORD);
    downSince = 0;
  }
}

void tickMqtt() {
  static uint32_t lastTry = 0;
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) { mqtt.loop(); return; }
  if (millis() - lastTry < MQTT_RETRY_MS) return;
  lastTry = millis();

  Serial.println("[MQTT] connecting...");
  // Last Will: retained "offline" on ungraceful disconnect
  bool ok = mqtt.connect(DEVICE_ID, g_mqttUser.c_str(), g_mqttPass.c_str(),
                         TOPIC_STATUS, 1, true, "{\"online\":false}");
  if (ok) {
    mqtt.publish(TOPIC_STATUS, "{\"online\":true}", true);   // retained
    Serial.println("[MQTT] connected");
  } else {
    Serial.printf("[MQTT] failed, rc=%d\n", mqtt.state());
  }
}

// ============================ setup / loop ===============================

void setup() {
  Serial.begin(115200);
  delay(50);                         // one-time serial settle only (pre-loop)
  g_bootMs = millis();

  loadMqttConfig();                  // NVS -> g_mqtt* (falls back to secrets.h)

  snprintf(TOPIC_TELEMETRY, sizeof(TOPIC_TELEMETRY), "aquaponics/%s/%s/telemetry", SITE_ID, DEVICE_ID);
  snprintf(TOPIC_STATUS,    sizeof(TOPIC_STATUS),    "aquaponics/%s/%s/status",    SITE_ID, DEVICE_ID);

  // RGB PWM
  ledcSetup(CH_R, LED_FREQ, LED_RES); ledcAttachPin(PIN_LED_R, CH_R);
  ledcSetup(CH_G, LED_FREQ, LED_RES); ledcAttachPin(PIN_LED_G, CH_G);
  ledcSetup(CH_B, LED_FREQ, LED_RES); ledcAttachPin(PIN_LED_B, CH_B);
  setRGB(0, 0, 0);

  // OLED
  u8g2.begin();
  u8g2.setBusClock(400000);
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(0, 20, "Aquaponics");
  u8g2.drawStr(0, 34, "booting...");
  u8g2.sendBuffer();

  // DHT
  dht.begin();

  // ---- Wi-Fi + MQTT provisioning (WiFiManager captive portal) ----
  pinMode(PIN_CONFIG_BTN, INPUT_PULLUP);

  {
    char portStr[6];
    snprintf(portStr, sizeof(portStr), "%u", g_mqttPort);
    p_host.setValue(g_mqttHost.c_str(), 64);
    p_port.setValue(portStr, 6);
    p_user.setValue(g_mqttUser.c_str(), 32);
    // password field intentionally left blank in the form
  }
  wm.addParameter(&p_host);
  wm.addParameter(&p_port);
  wm.addParameter(&p_user);
  wm.addParameter(&p_pass);
  wm.setSaveParamsCallback(saveParamsCallback);
  wm.setConfigPortalBlocking(false);              // portal runs from loop() via wm.process()
  wm.setConfigPortalTimeout(PORTAL_TIMEOUT_S);
  wm.setClass("invert");                          // dark portal UI

  // Portal trigger: hold BOOT for 3 s AFTER power-on (holding it DURING reset
  // enters the ROM bootloader, so we sample it here once the app is running).
  Serial.println("[CFG] hold BOOT within 3 s to open the setup portal...");
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(0, 14, "Hold BOOT for");
  u8g2.drawStr(0, 30, "setup  (3s)");
  u8g2.sendBuffer();
  bool forcePortal = false;
  for (uint32_t t0 = millis(); millis() - t0 < 3000; ) {
    if (digitalRead(PIN_CONFIG_BTN) == LOW) { forcePortal = true; break; }
    delay(20);                                    // one-time pre-loop wait, allowed
  }

  if (forcePortal) {
    Serial.println("[CFG] BOOT held -> starting config portal");
    wm.startConfigPortal(AP_NAME, AP_PASSWORD);
  } else {
    wm.autoConnect(AP_NAME, AP_PASSWORD);         // connect to saved AP, or open portal if none
  }

  // TLS: skip cert validation for first bring-up.
  // For production pin the CA:  netClient.setCACert(HIVEMQ_ROOT_CA);
  netClient.setInsecure();

  mqtt.setServer(g_mqttHost.c_str(), g_mqttPort);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);

  Serial.printf("[BOOT] setup done — MQTT %s:%u user=%s\n",
                g_mqttHost.c_str(), g_mqttPort, g_mqttUser.c_str());
}

void loop() {
  tickWiFi();
  tickMqtt();
  tickSensor();
  tickPublish();
  tickDisplay();
  tickLed();
  // no delay(): loop spins freely, each tick self-throttles on millis()
}
