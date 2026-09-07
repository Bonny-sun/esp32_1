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
//   * Backup Wi-Fi: the portal also takes an optional 2nd SSID/password
//     (e.g. a phone hotspot). Both APs are handed to WiFiMulti; tickWiFi()
//     retries whichever is in range if the primary drops.
//
//  Pixel pet note: the sprite is drawn procedurally (U8g2 primitives) instead of
//  a baked XBM byte-array. Reasons: trivial to swap facial expressions, no
//  288-byte/frame hex to maintain, less flash. To use hand-drawn art instead,
//  run tools/png_to_xbm.py on a 48x48 1-bit PNG and drawXBM() the result here.
// ============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>            // fallback: try backup SSID when primary AP is unreachable
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
WiFiMulti        wifiMulti;        // holds primary + backup AP so tickWiFi() can fall over
Preferences      prefs;

// Runtime MQTT config — loaded from NVS, falling back to secrets.h defaults.
String   g_mqttHost = MQTT_HOST;
uint16_t g_mqttPort = MQTT_PORT;
String   g_mqttUser = MQTT_USER;
String   g_mqttPass = MQTT_PASS;

// Backup Wi-Fi (2nd AP) — set via the captive portal, stored in NVS. The
// primary AP is still whatever WiFiManager's own SSID picker saved.
String g_wifiSsid2 = "";
String g_wifiPass2 = "";

// Captive-portal custom fields
WiFiManagerParameter p_host("host", "MQTT 主機位址", "", 64);
WiFiManagerParameter p_port("port", "MQTT 連接埠", "", 6);
WiFiManagerParameter p_user("user", "MQTT 帳號", "", 32);
WiFiManagerParameter p_pass("pass", "MQTT 密碼（留空＝不變更）", "", 64);
WiFiManagerParameter p_ssid2("ssid2", "備用 Wi-Fi 名稱（選填）", "", 32);
WiFiManagerParameter p_pass2("pass2", "備用 Wi-Fi 密碼（留空＝不變更）", "", 64);

// Pet skin picker: a raw-HTML <select> (no id) rather than the usual
// id-based WiFiManagerParameter, because WiFiManager always wraps id-based
// params in its <input> template — no way to get a <select> out of that.
// Built at runtime in setup() (needs g_petSkin already loaded from NVS to
// mark the right <option selected>), so the buffer must outlive setup();
// a static array does that. Read back in saveParamsCallback() via
// wm.server->arg("pet") since a no-id param never populates getValue().
static char petSelectHtml[320];
WiFiManagerParameter* p_petSelect = nullptr;

char TOPIC_TELEMETRY[80];
char TOPIC_STATUS[80];
char TOPIC_CMD[80];        // subscribed; remote pet-skin changes (dashboard -> MQTT -> here)

enum Mood { COMFY, HOT, COLD };
Mood  g_mood = COMFY;
float g_temp = NAN;
float g_hum  = NAN;

enum PetSkin { PET_DROP, PET_FISH, PET_CAT, PET_PANDA };
PetSkin g_petSkin = PET_DROP;   // loaded from NVS ("pet"), set via the portal
bool  g_timeSynced = false;
uint32_t g_lastPublishFlash = 0;   // millis() of last MQTT publish (for LED blip)
uint32_t g_bootMs = 0;

// Portal save -> deferred restart. We must NOT restart inside
// saveParamsCallback(): WiFiManager only runs WiFi.begin(newSsid,newPass)
// (which persists the creds to NVS) later, from wm.process(). Restarting in
// the callback reboots onto the OLD creds. Instead we flag it and let loop()
// restart once the new Wi-Fi has connected (or a grace timeout elapses).
bool     g_cfgSaved   = false;
uint32_t g_cfgSavedAt = 0;

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

PetSkin petSkinFromString(const String& s) {
  if (s == "fish")  return PET_FISH;
  if (s == "cat")   return PET_CAT;
  if (s == "panda") return PET_PANDA;
  return PET_DROP;
}

const char* petSkinToString(PetSkin s) {
  switch (s) {
    case PET_FISH:  return "fish";
    case PET_CAT:   return "cat";
    case PET_PANDA: return "panda";
    default:        return "drop";
  }
}

// ---- Runtime MQTT config (NVS <- portal, defaults <- secrets.h) --------
void loadMqttConfig() {
  prefs.begin("aqua", true);                       // read-only
  g_mqttHost = prefs.getString("host", MQTT_HOST);
  g_mqttPort = prefs.getUShort("port", MQTT_PORT);
  g_mqttUser = prefs.getString("user", MQTT_USER);
  g_mqttPass = prefs.getString("pass", MQTT_PASS);
  g_wifiSsid2 = prefs.getString("ssid2", "");
  g_wifiPass2 = prefs.getString("pass2", "");
  g_petSkin = petSkinFromString(prefs.getString("pet", "drop"));
  prefs.end();
}

// WiFiManager calls this after the user saves the portal form.
void saveParamsCallback() {
  prefs.begin("aqua", false);
  if (strlen(p_host.getValue())) prefs.putString("host", p_host.getValue());
  if (strlen(p_port.getValue())) prefs.putUShort("port", (uint16_t)atoi(p_port.getValue()));
  if (strlen(p_user.getValue())) prefs.putString("user", p_user.getValue());
  if (strlen(p_pass.getValue())) prefs.putString("pass", p_pass.getValue());
  if (strlen(p_ssid2.getValue())) prefs.putString("ssid2", p_ssid2.getValue());
  if (strlen(p_pass2.getValue())) prefs.putString("pass2", p_pass2.getValue());
  // p_petSelect has no id (it's a raw <select>, see its declaration above),
  // so WiFiManager never captures its value into a getValue() buffer —
  // read the submitted "pet" field straight off the live request instead.
  String pet = wm.server->arg("pet");
  if (pet.length()) prefs.putString("pet", pet);
  prefs.end();
  // Deferred restart — see g_cfgSaved. Restarting here would beat WiFiManager
  // to applying the new SSID/pass, so the board would reboot onto the old ones.
  Serial.println("[CFG] saved — restarting once Wi-Fi (re)connects");
  g_cfgSaved   = true;
  g_cfgSavedAt = millis();
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
// Three interchangeable skins, picked via the portal's "虛擬寵物外觀"
// dropdown (see p_petSelect) and persisted in NVS as g_petSkin. Each is a
// self-contained draw function so they can diverge freely; drawPet() just
// dispatches to whichever is active. All must leave the draw color at 1
// (white) on exit — drawStats() right after assumes that starting state.

void drawPetDrop() {
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

void drawPetFish() {
  const bool blink = (millis() % 4000) < 150;
  int dy = 0;
  if (g_mood == COLD) dy = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter (bob)
  const int cx = 24;
  const int cy = 26 + dy;

  // --- body: oval + tail fin (facing right) + dorsal fin ---
  u8g2.setDrawColor(1);
  u8g2.drawTriangle(cx - 14, cy - 8, cx - 14, cy + 8, cx - 24, cy);       // tail fin
  u8g2.drawFilledEllipse(cx, cy, 15, 11);                                 // body
  u8g2.drawTriangle(cx - 2, cy - 11, cx + 6, cy - 11, cx + 2, cy - 18);   // dorsal fin

  // --- face features punched in black on the white body ---
  u8g2.setDrawColor(0);

  // eyes
  if (g_mood == HOT) {                         // dizzy X eyes
    u8g2.drawLine(cx - 9, cy - 5, cx - 4, cy);   u8g2.drawLine(cx - 9, cy,     cx - 4, cy - 5);
    u8g2.drawLine(cx + 3, cy - 5, cx + 8, cy);   u8g2.drawLine(cx + 3, cy,     cx + 8, cy - 5);
  } else if (g_mood == COLD) {                 // squinting  u_u
    u8g2.drawLine(cx - 9, cy - 3, cx - 6, cy);   u8g2.drawLine(cx - 6, cy,     cx - 3, cy - 3);
    u8g2.drawLine(cx + 3, cy - 3, cx + 6, cy);   u8g2.drawLine(cx + 6, cy,     cx + 9, cy - 3);
  } else if (blink) {                          // COMFY blink
    u8g2.drawHLine(cx - 9, cy - 2, 6);
    u8g2.drawHLine(cx + 3, cy - 2, 6);
  } else {                                     // COMFY open eyes
    u8g2.drawDisc(cx - 6, cy - 2, 2);
    u8g2.drawDisc(cx + 6, cy - 2, 2);
  }

  // mouth: fish always looks a little "o" surprised
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, cy + 6, 3);
  } else if (g_mood == COLD) {
    u8g2.drawBox(cx - 3, cy + 5, 6, 3);
  } else {
    u8g2.drawDisc(cx, cy + 6, 2);
  }

  // --- extras (white) ---
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {
    int sy = 4 + (int)((millis() / 150) % 8);          // rising bubble
    u8g2.drawDisc(cx + 18, sy, 2);
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, 20, 6, 18);  u8g2.drawLine(2, 24, 6, 22);   // shiver marks
    u8g2.drawLine(40, 20, 44, 22); u8g2.drawLine(40, 24, 44, 26);
  }
}

// Cat and panda share this "cute portrait" template — big round head, ears
// as an overlapping circle so the silhouette stays smooth (no separate
// pointy shape to look "off"), big simple dot eyes, minimal mouth, no
// body/tail/whiskers. Only the ear fill and a tiny nose differ between them.
void drawPetCat() {
  const bool blink = (millis() % 4000) < 150;
  int dx = 0;
  if (g_mood == COLD) dx = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter
  const int cx = 23 + dx;
  const int cy = 30;

  // --- head + ears: same fill as the head, so they melt into one smooth
  // rounded silhouette instead of reading as separate spikes ---
  u8g2.setDrawColor(1);
  u8g2.drawDisc(cx, cy, 15);
  u8g2.drawDisc(cx - 10, 14, 7);
  u8g2.drawDisc(cx + 10, 14, 7);

  // --- face, punched in black ---
  u8g2.setDrawColor(0);
  if (g_mood == HOT) {                         // dizzy X eyes
    u8g2.drawLine(cx - 10, cy - 5, cx - 4, cy + 1); u8g2.drawLine(cx - 10, cy + 1, cx - 4, cy - 5);
    u8g2.drawLine(cx + 4,  cy - 5, cx + 10, cy + 1); u8g2.drawLine(cx + 4,  cy + 1, cx + 10, cy - 5);
  } else {                                     // COMFY / COLD share open-eyed base
    if (blink) {
      u8g2.drawHLine(cx - 10, cy - 2, 6);
      u8g2.drawHLine(cx + 4,  cy - 2, 6);
    } else {
      u8g2.drawDisc(cx - 7, cy - 2, 3);
      u8g2.drawDisc(cx + 7, cy - 2, 3);
    }
    if (g_mood == COLD) {                      // angry/cold eyebrows
      u8g2.drawLine(cx - 10, cy - 7, cx - 4, cy - 4);
      u8g2.drawLine(cx + 4,  cy - 4, cx + 10, cy - 7);
    }
  }

  // mouth
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, cy + 9, 3);                                    // panting
  } else if (g_mood == COLD) {                                       // gritted zigzag
    u8g2.drawLine(cx - 4, cy + 7, cx - 2, cy + 9); u8g2.drawLine(cx - 2, cy + 9, cx, cy + 7);
    u8g2.drawLine(cx,     cy + 7, cx + 2, cy + 9); u8g2.drawLine(cx + 2, cy + 9, cx + 4, cy + 7);
  } else {
    u8g2.drawLine(cx - 4, cy + 7, cx, cy + 5); u8g2.drawLine(cx, cy + 5, cx + 4, cy + 7);  // smile
  }

  // --- mood extras (white) ---
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {
    int sy = 2 + (int)((millis() / 150) % 8);
    u8g2.drawDisc(cx + 15, sy, 2);                                    // sweat drop
    u8g2.drawLine(cx - 14, 4, cx - 11, 2); u8g2.drawLine(cx - 11, 2, cx - 8, 4);  // heat wiggle
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, cy - 6, 6, cy - 8);  u8g2.drawLine(2, cy - 2, 6, cy - 4);    // shiver marks
    u8g2.drawLine(40, cy - 6, 44, cy - 4); u8g2.drawLine(40, cy - 2, 44, cy);
    u8g2.drawTriangle(cx - 13, 16, cx - 9, 16, cx - 11, 22);          // icicles off the ears
    u8g2.drawTriangle(cx + 9,  16, cx + 13, 16, cx + 11, 22);
  }
}

void drawPetPanda() {
  const bool blink = (millis() % 4000) < 150;
  int dx = 0;
  if (g_mood == COLD) dx = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter
  const int cx = 23 + dx;
  const int cy = 30;

  // --- head, then black ears sunk mostly into it. The visible edge is only
  // the black-on-white boundary where they overlap the head; the rest of
  // each ear circle blends into the (also black) background, same as the
  // reference art — no white ring needed. ---
  u8g2.setDrawColor(1);
  u8g2.drawDisc(cx, cy, 15);
  u8g2.setDrawColor(0);
  u8g2.drawDisc(cx - 11, 16, 8);
  u8g2.drawDisc(cx + 11, 16, 8);

  // --- face, punched in black — identical template to the cat, plus a
  // tiny nose, since that's the only other panda-vs-cat cue in the ref ---
  u8g2.setDrawColor(0);
  if (g_mood == HOT) {                         // dizzy X eyes
    u8g2.drawLine(cx - 10, cy - 5, cx - 4, cy + 1); u8g2.drawLine(cx - 10, cy + 1, cx - 4, cy - 5);
    u8g2.drawLine(cx + 4,  cy - 5, cx + 10, cy + 1); u8g2.drawLine(cx + 4,  cy + 1, cx + 10, cy - 5);
  } else {                                     // COMFY / COLD share open-eyed base
    if (blink) {
      u8g2.drawHLine(cx - 10, cy - 2, 6);
      u8g2.drawHLine(cx + 4,  cy - 2, 6);
    } else {
      u8g2.drawDisc(cx - 7, cy - 2, 3);
      u8g2.drawDisc(cx + 7, cy - 2, 3);
    }
    if (g_mood == COLD) {                      // angry/cold eyebrows
      u8g2.drawLine(cx - 10, cy - 7, cx - 4, cy - 4);
      u8g2.drawLine(cx + 4,  cy - 4, cx + 10, cy - 7);
    }
  }
  u8g2.drawDisc(cx, cy + 1, 1);                 // nose

  // mouth
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, cy + 9, 3);                                    // panting
  } else if (g_mood == COLD) {                                       // gritted zigzag
    u8g2.drawLine(cx - 4, cy + 7, cx - 2, cy + 9); u8g2.drawLine(cx - 2, cy + 9, cx, cy + 7);
    u8g2.drawLine(cx,     cy + 7, cx + 2, cy + 9); u8g2.drawLine(cx + 2, cy + 9, cx + 4, cy + 7);
  } else {
    u8g2.drawLine(cx - 4, cy + 7, cx, cy + 5); u8g2.drawLine(cx, cy + 5, cx + 4, cy + 7);  // smile
  }

  // --- mood extras (white) ---
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {
    int sy = 2 + (int)((millis() / 150) % 8);
    u8g2.drawDisc(cx + 15, sy, 2);                                    // sweat drop
    u8g2.drawLine(cx - 14, 4, cx - 11, 2); u8g2.drawLine(cx - 11, 2, cx - 8, 4);  // heat wiggle
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, cy - 6, 6, cy - 8);  u8g2.drawLine(2, cy - 2, 6, cy - 4);    // shiver marks
    u8g2.drawLine(40, cy - 6, 44, cy - 4); u8g2.drawLine(40, cy - 2, 44, cy);
    u8g2.drawTriangle(cx - 14, 17, cx - 10, 17, cx - 12, 23);         // icicles off the ears
    u8g2.drawTriangle(cx + 10, 17, cx + 14, 17, cx + 12, 23);
  }
}

void drawPet() {
  switch (g_petSkin) {
    case PET_FISH:  drawPetFish();  break;
    case PET_CAT:   drawPetCat();   break;
    case PET_PANDA: drawPetPanda(); break;
    default:        drawPetDrop();  break;
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

  doc["pet"] = petSkinToString(g_petSkin);  // lets the dashboard show what's actually applied

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
  static uint32_t lastRetry = 0;
  wm.process();                          // services the captive portal when active; no-op otherwise

  if (WiFi.status() == WL_CONNECTED) {
    downSince = 0;
    if (!g_timeSynced) {
      configTzTime("CST-8", "pool.ntp.org", "time.nist.gov");  // local = UTC+8 (Taiwan, no DST); MQTT ts stays UTC via gmtime_r
      g_timeSynced = true;                                     // NTP fills in async
    }
    return;
  }
  // Not connected: the ESP32 core auto-reconnects to the saved primary AP; if a
  // backup SSID was set via the portal, wifiMulti additionally retries that one
  // (whichever AP is in range wins). This call can briefly stall (short scan),
  // so it's throttled like the MQTT retry below rather than run every loop().
  if (!wm.getConfigPortalActive() && millis() - lastRetry > WIFI_RETRY_MS) {
    lastRetry = millis();
    if (g_wifiSsid2.length()) wifiMulti.run(WIFI_RETRY_MS);
  }

  // If Wi-Fi stays down for a long stretch, reopen the portal so creds can be
  // fixed on-site without a laptop.
  if (downSince == 0) downSince = millis();
  if (!wm.getConfigPortalActive() && millis() - downSince > 120000UL) {
    Serial.println("[WiFi] down >2 min — opening config portal");
    wm.startConfigPortal(AP_NAME, AP_PASSWORD);
    downSince = 0;
  }
}

// Remote pet-skin change: dashboard publishes a retained {"pet":"cat"} to
// TOPIC_CMD. Applied live (no reboot — drawPet() just reads g_petSkin every
// frame) and persisted to NVS so it also survives one.
void mqttCallback(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;   // malformed, ignore
  if (doc["pet"].isNull()) return;

  PetSkin skin = petSkinFromString(doc["pet"].as<String>());
  g_petSkin = skin;
  prefs.begin("aqua", false);
  prefs.putString("pet", petSkinToString(skin));
  prefs.end();
  Serial.printf("[MQTT] pet skin -> %s\n", petSkinToString(skin));
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
    mqtt.subscribe(TOPIC_CMD);
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
  snprintf(TOPIC_CMD,       sizeof(TOPIC_CMD),       "aquaponics/%s/%s/cmd",       SITE_ID, DEVICE_ID);

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
    // password fields intentionally left blank in the form
    p_ssid2.setValue(g_wifiSsid2.c_str(), 32);
  }
  {
    // Built here (after g_petSkin is loaded) so the right <option> starts
    // pre-selected. petSelectHtml is static, so this pointer stays valid
    // for the portal's whole lifetime.
    snprintf(petSelectHtml, sizeof(petSelectHtml),
      "<br/><label>虛擬寵物外觀</label><br/>"
      "<select name='pet'>"
      "<option value='drop' %s>水滴</option>"
      "<option value='fish' %s>魚</option>"
      "<option value='cat' %s>貓</option>"
      "<option value='panda' %s>熊貓</option>"
      "</select><br/>",
      g_petSkin == PET_DROP  ? "selected" : "",
      g_petSkin == PET_FISH  ? "selected" : "",
      g_petSkin == PET_CAT   ? "selected" : "",
      g_petSkin == PET_PANDA ? "selected" : "");
    p_petSelect = new WiFiManagerParameter(petSelectHtml);
  }
  wm.addParameter(&p_host);
  wm.addParameter(&p_port);
  wm.addParameter(&p_user);
  wm.addParameter(&p_pass);
  wm.addParameter(&p_ssid2);
  wm.addParameter(&p_pass2);
  wm.addParameter(p_petSelect);
  wm.setSaveParamsCallback(saveParamsCallback);
  wm.setConfigPortalBlocking(false);              // portal runs from loop() via wm.process()
  wm.setConfigPortalTimeout(PORTAL_TIMEOUT_S);
  wm.setClass("invert");                          // dark portal UI
  wm.setTitle("AquaGuardian 設定");                // browser tab title + page header

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

  // Register both APs with wifiMulti so tickWiFi() can fail over to the
  // backup network later. WiFi.psk() reads back the primary's passphrase
  // that the ESP32 core just saved to NVS (Arduino-ESP32 extension, not
  // available on plain WiFiSTAClass elsewhere).
  if (WiFi.status() == WL_CONNECTED) {
    wifiMulti.addAP(WiFi.SSID().c_str(), WiFi.psk().c_str());
  }
  if (g_wifiSsid2.length()) {
    wifiMulti.addAP(g_wifiSsid2.c_str(), g_wifiPass2.c_str());
  }

  // TLS: skip cert validation for first bring-up.
  // For production pin the CA:  netClient.setCACert(HIVEMQ_ROOT_CA);
  netClient.setInsecure();

  mqtt.setServer(g_mqttHost.c_str(), g_mqttPort);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);

  Serial.printf("[BOOT] setup done — MQTT %s:%u user=%s\n",
                g_mqttHost.c_str(), g_mqttPort, g_mqttUser.c_str());
}

void loop() {
  tickWiFi();

  // Deferred post-portal restart: reboot once the newly-entered Wi-Fi has
  // actually connected (creds are now in NVS), or after a 15 s grace period
  // if it can't (e.g. wrong password) — either way the creds are persisted
  // and a fresh boot gives the cleanest re-init.
  if (g_cfgSaved && (WiFi.status() == WL_CONNECTED || millis() - g_cfgSavedAt > 15000UL)) {
    Serial.println("[CFG] applying new config — restarting now");
    delay(200);
    ESP.restart();
  }

  tickMqtt();
  tickSensor();
  tickPublish();
  tickDisplay();
  tickLed();
  // no delay(): loop spins freely, each tick self-throttles on millis()
}
