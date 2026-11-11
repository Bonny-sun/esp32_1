// ╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗
//
// 類別：裝置端（Device / Firmware）── 燒錄在 ESP32 開發板上執行的韌體，用 C++ 寫成。
// 目的：ESP32 接上溫濕度感測器、OLED 螢幕、RGB 燈，共做 4 件事：
//       1. 每 2 秒讀一次溫濕度
//       2. 螢幕左邊用一隻「虛擬寵物」(水滴/魚/貓/熊貓)表達冷熱心情，右邊顯示數值與時間
//       3. 每 60 秒把資料用 MQTT 送到雲端（再由後端 worker 存進資料庫）
//       4. 可用手機連網頁設定 Wi-Fi，也能從儀表板遠端換寵物外觀、改冷熱門檻
// 資料流向：感測器(DHT) ──> 【本程式】 ──MQTT──> 雲端後端
//           儀表板網頁 ──MQTT 指令──> 【本程式】（換寵物、改門檻）
//
// 程式架構：
//   一、設定與常數          【1-1】引入函式庫  【1-2】密碼檔 secrets.h  【1-3】腳位  【1-4】寵物冷熱門檻
//                           【1-5】時間間隔  【1-6】RGB 燈 PWM  【1-7】設定入口網頁  【1-8】HiveMQ 憑證
//   二、全域變數            【2-1】硬體與連線物件  【2-2】可被網頁/雲端修改的設定  【2-3】設定網頁欄位
//                           【2-4】MQTT 主題  【2-5】心情與寵物狀態  【2-6】存檔後延遲重啟旗標
//   三、共用小工具          【3-1】四捨五入  【3-2】控制 RGB 燈  【3-3】時間字串  【3-4】寵物外觀 ⇄ 文字
//   四、設定的儲存與讀取    【4-1】開機讀取設定  【4-2】網頁按存檔時儲存
//   五、寵物心情判斷        【5-1】依溫度決定 舒適 / 太熱 / 太冷
//   六、寵物圖形(螢幕左側)  【6-1】水滴  【6-2】魚  【6-3】貓  【6-4】熊貓  【6-5】依外觀選擇要畫哪隻
//   七、資訊面板(螢幕右側)  【7-1】Wi-Fi 訊號格  【7-2】MQTT 圓點  【7-3】時鐘  【7-4】溫濕度  【7-5】狀態文字
//   八、週期工作 tick       【8-1】讀感測器  【8-2】設定模式畫面  【8-3】重畫螢幕  【8-4】LED 呼吸燈
//                           【8-5】組裝並送出資料  【8-6】定時發布
//   九、連線維護            【9-1】Wi-Fi 重連與備用網路  【9-2】收到雲端指令  【9-3】MQTT 重連
//   十、程式入口            【10-1】讀設定、組主題  【10-2】初始化硬體  【10-3】填入設定網頁欄位
//                           【10-4】設定入口與 BOOT 鍵偵測  【10-5】連 Wi-Fi、登記備用網路
//                           【10-6】設定 MQTT 連線  【10-7】loop 主迴圈
//
// 備註：
//   換/改寵物圖形 ........ 第六項（每隻寵物各一個函式）
//   改冷熱門檻預設值 ..... 【1-4】
//   改多久送一次資料 ..... 【1-5】的 PUBLISH_MS
//   換感測器型號 ......... 【1-3】的 DHT_TYPE
//   改螢幕右側排版 ....... 第七項
//   改 LED 燈色 .......... 【8-4】
//   名詞：NVS = ESP32 內建的「斷電也不會消失」的小記憶體，用來存設定（見第四項）
//         tick  = 「時間到了才做」的小工作，每圈主迴圈都問一次（見第八項）
//
// ╚════════════════════════════════════════════════════════════════════════════╝

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
//   * Multi-board: the portal also has a "裝置 ID" field. Flash this one image
//     to every board and give each a unique id (esp32-aqua-02, ...) in the
//     portal — no per-board build. Defaults to secrets.h DEVICE_ID.
//   * Backup Wi-Fi: the portal also takes an optional 2nd SSID/password
//     (e.g. a phone hotspot). Both APs are handed to WiFiMulti; tickWiFi()
//     retries whichever is in range if the primary drops.
//
//  Pixel pet note: the sprite is drawn procedurally (U8g2 primitives) instead of
//  a baked XBM byte-array. Reasons: trivial to swap facial expressions, no
//  288-byte/frame hex to maintain, less flash. To use hand-drawn art instead,
//  run tools/png_to_xbm.py on a 48x48 1-bit PNG and drawXBM() the result here.
// ============================================================================

// ============================================================================
// 一、設定與常數：腳位、時間間隔、門檻等「寫死的數字」都在這裡，要調整先來這找
// ============================================================================

// ---- 【1-1】引入函式庫（WiFi、MQTT、螢幕、感測器等現成工具）----
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

// ---- 【1-2】密碼檔 secrets.h（Wi-Fi/MQTT 帳密的「預設值」；找不到檔案就用下方假值）----
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

// ---- 【1-3】腳位對照：哪個零件接在 ESP32 的哪一支腳（GPIO 編號）----
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

// ---- 【1-4】寵物冷熱門檻（攝氏）：高於 HOT 變「太熱」，低於 COLD 變「太冷」----
// Runtime-configurable from the dashboard: it publishes retained
// {"pet_hot":X,"pet_cold":Y} to .../cmd; the values live in NVS.
static const float PET_HOT_DEFAULT  = 28.0f;
static const float PET_COLD_DEFAULT = 18.0f;
static const float TEMP_HYST        = 1.0f;   // must recover by this much to return COMFY

// ---- 【1-5】時間間隔（毫秒，1000 毫秒 = 1 秒）：各個 tick 多久做一次 ----
static const uint32_t SENSOR_MS   = 2000;    // DHT11 max sample rate ~1 Hz
static const uint32_t DISPLAY_MS  = 100;     // ~10 fps redraw (blink/shiver)
static const uint32_t LED_MS      = 20;      // breathing LED update
static const uint32_t PUBLISH_MS  = 120000;  // MQTT telemetry interval (2 min -> ~720 rows/day)
static const uint32_t WIFI_RETRY_MS = 5000;
static const uint32_t MQTT_RETRY_MS = 3000;

// ---- 【1-6】RGB 燈的 PWM 設定（用「開關很快」來調亮度，0~255）----
static const int CH_R = 0, CH_G = 1, CH_B = 2;
static const int LED_FREQ = 5000, LED_RES = 8;   // 8-bit: 0..255

// ---- 【1-7】設定入口網頁（手機連上 ESP32 自己開的 Wi-Fi 來設定帳密）----
static const char*    AP_NAME          = "AquaGuardian-Setup";
static const char*    AP_PASSWORD      = "aquaguardian";  // >= 8 chars; WPA2 on the setup AP (open APs are flaky on Android)
static const int      PIN_CONFIG_BTN   = 0;      // BOOT button — hold 3 s AFTER power-on to force the portal
static const uint16_t PORTAL_TIMEOUT_S = 180;    // portal auto-closes after this idle time

// ---- 【1-8】HiveMQ Cloud 的信任根憑證（驗證 MQTT TLS 連線用的，不是帳密，可以公開）----
// HiveMQ Cloud 的伺服器憑證是由 Amazon 簽發的（他們的雲端主機架在 AWS 上），
// 所以這裡放 Amazon 官方公開的 Root CA 1，原文取自：
// https://www.amazontrust.com/repository/AmazonRootCA1.pem （沒有做任何修改）
static const char HIVEMQ_ROOT_CA[] = R"EOF(
-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL
MAkGA1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJv
b3QgQ0EgMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALJ4gHHKeNXj
ca9HgFB0fW7Y14h29Jlo91ghYPl0hAEvrAIthtOgQ3pOsqTQNroBvo3bSMgHFzZM
9O6II8c+6zf1tRn4SWiw3te5djgdYZ6k/oI2peVKVuRF4fn9tBb6dNqcmzU5L/qw
IFAGbHrQgLKm+a/sRxmPUDgH3KKHOVj4utWp+UhnMJbulHheb4mjUcAwhmahRWa6
VOujw5H5SNz/0egwLX0tdHA114gk957EWW67c4cX8jJGKLhD+rcdqsq08p8kDi1L
93FcXmn/6pUCyziKrlA4b9v7LWIbxcceVOF34GfID5yHI9Y/QCB/IIDEgEw+OyQm
jgSubJrIqg0CAwEAAaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMC
AYYwHQYDVR0OBBYEFIQYzIU07LwMlJQuCFmcx7IQTgoIMA0GCSqGSIb3DQEBCwUA
A4IBAQCY8jdaQZChGsV2USggNiMOruYou6r4lK5IpDB/G/wkjUu0yKGX9rbxenDI
U5PMCCjjmCXPI6T53iHTfIUJrU6adTrCC2qJeHZERxhlbI1Bjjt/msv0tadQ1wUs
N+gDS63pYaACbvXy8MWy7Vu33PqUXHeeE6V/Uq2V8viTO96LXFvKWlJbYK8U90vv
o/ufQJVtMVT8QtPHRh8jrdkPSHCa2XV4cdFyQzR1bldZwgJcJmApzyMZFo6IQ6XU
5MsI+yMRQ+hDKXJioaldXgjUkK642M4UwtBV8ob2xJNDd2ZhwLnoQdeXeGADbkpy
rqXRfboQnoZsG4q5WTP468SQvvG5
-----END CERTIFICATE-----
)EOF";

// ============================================================================
// 二、全域變數：整支程式共用的物件與狀態（變數名 g_ 開頭 = 全域 global）
// ============================================================================

// ---- 【2-1】硬體與連線物件：螢幕、感測器、WiFi、MQTT、設定入口、NVS ----
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, PIN_SCL, PIN_SDA);
DHT dht(PIN_DHT, DHT_TYPE);

WiFiClientSecure netClient;
PubSubClient     mqtt(netClient);
WiFiManager      wm;
WiFiMulti        wifiMulti;        // holds primary + backup AP so tickWiFi() can fall over
Preferences      prefs;

// ---- 【2-2】可被網頁/雲端修改的設定：開機時從 NVS 讀入，沒存過就用 secrets.h 預設 ----
// Runtime config — loaded from NVS, falling back to secrets.h defaults.
// g_deviceId lets one firmware image serve many boards: flash it, then set a
// unique ID (e.g. esp32-aqua-02) in the captive portal. Defaults to the
// secrets.h DEVICE_ID so an already-provisioned board is unaffected.
String   g_deviceId = DEVICE_ID;
String   g_mqttHost = MQTT_HOST;
uint16_t g_mqttPort = MQTT_PORT;
String   g_mqttUser = MQTT_USER;
String   g_mqttPass = MQTT_PASS;
float    g_tempHot  = PET_HOT_DEFAULT;   // pet HOT/COLD thresholds, from NVS / dashboard
float    g_tempCold = PET_COLD_DEFAULT;

// Backup Wi-Fi (2nd AP) — set via the captive portal, stored in NVS. The
// primary AP is still whatever WiFiManager's own SSID picker saved.
String g_wifiSsid2 = "";
String g_wifiPass2 = "";

// ---- 【2-3】設定網頁上自訂的欄位（裝置 ID、MQTT 帳密、備用 Wi-Fi、寵物下拉選單）----
// Captive-portal custom fields
WiFiManagerParameter p_devid("devid", "裝置 ID（每台唯一，如 esp32-aqua-02）", "", 40);
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

// ---- 【2-4】MQTT 主題（雲端信箱的「地址」，在 setup 裡用站點/裝置 ID 組出來）----
// telemetry = 送出感測資料；status = 上線/離線；cmd = 接收雲端指令
char TOPIC_TELEMETRY[80];
char TOPIC_STATUS[80];
char TOPIC_CMD[80];        // subscribed; remote pet-skin changes (dashboard -> MQTT -> here)

// ---- 【2-5】心情與寵物狀態：目前心情(舒適/熱/冷)、溫濕度、寵物外觀、各種計時用變數 ----
enum Mood { COMFY, HOT, COLD };
Mood  g_mood = COMFY;
float g_temp = NAN;
float g_hum  = NAN;

enum PetSkin { PET_DROP, PET_FISH, PET_CAT, PET_PANDA };
PetSkin g_petSkin = PET_DROP;   // loaded from NVS ("pet"), set via the portal
bool  g_timeSynced = false;
uint32_t g_lastPublishFlash = 0;   // millis() of last MQTT publish (for LED blip)
uint32_t g_bootMs = 0;

// ---- 【2-6】設定存檔後「延遲重啟」的旗標（為什麼不能立刻重啟，見下方說明）----
// Portal save -> deferred restart. We must NOT restart inside
// saveParamsCallback(): WiFiManager only runs WiFi.begin(newSsid,newPass)
// (which persists the creds to NVS) later, from wm.process(). Restarting in
// the callback reboots onto the OLD creds. Instead we flag it and let loop()
// restart once the new Wi-Fi has connected (or a grace timeout elapses).
bool     g_cfgSaved   = false;
uint32_t g_cfgSavedAt = 0;

// ============================================================================
// 三、共用小工具：到處都會用到的小函式
// ============================================================================

// ---- 【3-1】四捨五入到小數 1 位（溫濕度送出前用）----
static inline float round1(float v) { return roundf(v * 10.0f) / 10.0f; }

// ---- 【3-2】設定 RGB 燈的顏色（r/g/b 各 0~255）----
void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  // common cathode -> higher duty = brighter
  ledcWrite(CH_R, r);
  ledcWrite(CH_G, g);
  ledcWrite(CH_B, b);
}

// ---- 【3-3】時間字串：isoTimestamp() 給雲端用(UTC)；localHHMM() 給螢幕用(台北時間)----
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

// ---- 【3-4】寵物外觀 ⇄ 文字轉換："cat" ⇄ PET_CAT（存檔、雲端指令都用文字）----
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

// ============================================================================
// 四、設定的儲存與讀取（NVS）：設定網頁填的資料存進 NVS，斷電重開也還在
// ============================================================================

// ---- 【4-1】開機時讀取設定：NVS 有存就用存的，沒有就用 secrets.h 的預設值 ----
// ---- Runtime MQTT config (NVS <- portal, defaults <- secrets.h) --------
void loadMqttConfig() {
  prefs.begin("aqua", true);                       // read-only
  g_deviceId = prefs.getString("devid", DEVICE_ID);
  g_mqttHost = prefs.getString("host", MQTT_HOST);
  g_mqttPort = prefs.getUShort("port", MQTT_PORT);
  g_mqttUser = prefs.getString("user", MQTT_USER);
  g_mqttPass = prefs.getString("pass", MQTT_PASS);
  g_wifiSsid2 = prefs.getString("ssid2", "");
  g_wifiPass2 = prefs.getString("pass2", "");
  g_petSkin = petSkinFromString(prefs.getString("pet", "drop"));
  g_tempHot  = prefs.getFloat("thi", PET_HOT_DEFAULT);
  g_tempCold = prefs.getFloat("tlo", PET_COLD_DEFAULT);
  prefs.end();
}

// ---- 【4-2】使用者在設定網頁按「存檔」時執行：把有填的欄位存進 NVS（沒填的保持不變）----
// WiFiManager calls this after the user saves the portal form.
void saveParamsCallback() {
  prefs.begin("aqua", false);
  if (strlen(p_devid.getValue())) prefs.putString("devid", p_devid.getValue());
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

// ============================================================================
// 五、寵物心情判斷
// ============================================================================

// ---- 【5-1】依溫度決定心情：舒適 → (太熱|太冷)，回到舒適要多退 1 度才算（避免在門檻邊緣一直跳）----
void updateMood(float t) {
  if (isnan(t)) return;
  switch (g_mood) {
    case COMFY:
      if      (t > g_tempHot)  g_mood = HOT;
      else if (t < g_tempCold) g_mood = COLD;
      break;
    case HOT:
      if (t < g_tempHot - TEMP_HYST)  g_mood = COMFY;
      break;
    case COLD:
      if (t > g_tempCold + TEMP_HYST) g_mood = COMFY;
      break;
  }
}

// ============================================================================
// 六、寵物圖形（螢幕左側 48x48 像素）：用畫線、畫圓的指令「現場畫」出來，不是圖檔
//     共 4 種外觀，每種一個獨立函式；【6-5】drawPet() 依目前外觀挑一個來畫。
//
//     看懂畫圖指令的 3 個重點：
//       1. setDrawColor(1) = 用白色畫；setDrawColor(0) = 用黑色畫（把白色身體「挖洞」成臉）
//       2. 每隻寵物的畫法順序都一樣：身體(白) → 眼睛 → 嘴巴 → 身體外的小配件(汗滴、發抖線)
//       3. 眼睛/嘴巴/配件會依心情 g_mood 換樣子：太熱 = X 眼睛、太冷 = 發抖
//     想改圖形：座標以螢幕左上角為(0,0)，往右 x 增加、往下 y 增加。
// ============================================================================
// Four interchangeable skins (drop/fish/cat/panda), picked via the portal's "虛擬寵物外觀"
// dropdown (see p_petSelect) and persisted in NVS as g_petSkin. Each is a
// self-contained draw function so they can diverge freely; drawPet() just
// dispatches to whichever is active. All must leave the draw color at 1
// (white) on exit — drawStats() right after assumes that starting state.

// ---- 【6-1】水滴：尖頭三角形 + 圓形底部組成水滴身體 ----
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

// ---- 【6-2】魚：橢圓身體 + 尾鰭 + 背鰭，嘴巴永遠有點驚訝的「o」形 ----
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

// ---- 【6-3】貓：圓頭 + 尖耳朵 + 鬍鬚 ----
// --- Cat: round head, POINTED triangular ears, and white whiskers that
// stick out past the face into the black background — the two cues that
// actually make a 48px blob read as "cat". ---
void drawPetCat() {
  const bool blink = (millis() % 4000) < 150;
  int dx = 0;
  if (g_mood == COLD) dx = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter
  const int cx = 23 + dx;
  const int cy = 32;

  u8g2.setDrawColor(1);
  // pointed ears (white), overlapping the head top
  u8g2.drawTriangle(cx - 15, 25, cx - 3, 25, cx - 13, 9);
  u8g2.drawTriangle(cx + 3, 25, cx + 15, 25, cx + 13, 9);
  u8g2.drawDisc(cx, cy, 13);                                 // head

  // inner ears (black)
  u8g2.setDrawColor(0);
  u8g2.drawTriangle(cx - 12, 23, cx - 6, 23, cx - 11, 13);
  u8g2.drawTriangle(cx + 6, 23, cx + 12, 23, cx + 11, 13);

  // eyes
  if (g_mood == HOT) {                                       // dizzy X
    u8g2.drawLine(cx - 10, cy - 5, cx - 4, cy + 1); u8g2.drawLine(cx - 10, cy + 1, cx - 4, cy - 5);
    u8g2.drawLine(cx + 4, cy - 5, cx + 10, cy + 1);  u8g2.drawLine(cx + 4, cy + 1, cx + 10, cy - 5);
  } else if (blink) {
    u8g2.drawHLine(cx - 10, cy - 2, 6);
    u8g2.drawHLine(cx + 4, cy - 2, 6);
  } else {
    u8g2.drawDisc(cx - 7, cy - 2, 3);
    u8g2.drawDisc(cx + 7, cy - 2, 3);
  }
  if (g_mood == COLD) {                                      // frown brows
    u8g2.drawLine(cx - 11, cy - 8, cx - 4, cy - 5);
    u8g2.drawLine(cx + 4, cy - 5, cx + 11, cy - 8);
  }

  // nose (little triangle) + mouth
  u8g2.drawTriangle(cx - 2, cy + 3, cx + 2, cy + 3, cx, cy + 6);
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, cy + 9, 2);
  } else {                                                   // ω mouth
    u8g2.drawLine(cx, cy + 6, cx - 3, cy + 9);
    u8g2.drawLine(cx, cy + 6, cx + 3, cy + 9);
  }

  // whiskers — white, out past the cheeks
  u8g2.setDrawColor(1);
  for (int i = -1; i <= 1; i++) {
    u8g2.drawLine(cx - 9, cy + 2 + i * 3, cx - 21, cy + i * 5);
    u8g2.drawLine(cx + 9, cy + 2 + i * 3, cx + 21, cy + i * 5);
  }

  // mood extras (white)
  if (g_mood == HOT) {
    int sy = 2 + (int)((millis() / 150) % 8);
    u8g2.drawDisc(cx + 16, sy, 2);
    u8g2.drawLine(cx - 15, 4, cx - 12, 2); u8g2.drawLine(cx - 12, 2, cx - 9, 4);
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, cy - 4, 6, cy - 6);  u8g2.drawLine(2, cy + 1, 6, cy - 1);
    u8g2.drawLine(40, cy - 4, 44, cy - 2); u8g2.drawLine(40, cy + 1, 44, cy + 3);
  }
}

// ---- 【6-4】熊貓：白臉 + 黑圓耳朵 + 黑眼圈 ----
// --- Panda: white head, round BLACK ears with a thin white rim (so they
// read on the black background), and the signature tilted BLACK eye
// patches with a white pupil dot inside each. ---
void drawPetPanda() {
  const bool blink = (millis() % 4000) < 150;
  int dx = 0;
  if (g_mood == COLD) dx = ((millis() / 70) % 2) ? -1 : 1;   // shiver jitter
  const int cx = 23 + dx;
  const int cy = 32;

  u8g2.setDrawColor(1);
  u8g2.drawDisc(cx, cy, 14);                                 // head
  u8g2.drawDisc(cx - 12, 15, 6);                             // ear discs (white rim)
  u8g2.drawDisc(cx + 12, 15, 6);
  u8g2.setDrawColor(0);
  u8g2.drawDisc(cx - 12, 15, 4);                             // ear cores (black)
  u8g2.drawDisc(cx + 12, 15, 4);

  // eye patches — black, teardrop tilted down-and-outward
  u8g2.drawDisc(cx - 6, cy - 3, 5);  u8g2.drawDisc(cx - 9, cy + 1, 3);
  u8g2.drawDisc(cx + 6, cy - 3, 5);  u8g2.drawDisc(cx + 9, cy + 1, 3);

  // pupils / eye state — white, inside the patches
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {                                       // dizzy X
    u8g2.drawLine(cx - 8, cy - 5, cx - 4, cy - 1); u8g2.drawLine(cx - 8, cy - 1, cx - 4, cy - 5);
    u8g2.drawLine(cx + 4, cy - 5, cx + 8, cy - 1); u8g2.drawLine(cx + 4, cy - 1, cx + 8, cy - 5);
  } else if (!blink) {                                       // (blink: no dot -> looks shut)
    u8g2.drawDisc(cx - 6, cy - 3, 2);
    u8g2.drawDisc(cx + 6, cy - 3, 2);
  }

  // nose + mouth (black)
  u8g2.setDrawColor(0);
  u8g2.drawDisc(cx, cy + 5, 2);
  u8g2.drawVLine(cx, cy + 6, 3);
  if (g_mood == HOT) {
    u8g2.drawDisc(cx, cy + 11, 2);
  } else if (g_mood == COLD) {
    u8g2.drawBox(cx - 3, cy + 9, 6, 2);                      // flat gritted mouth
  } else {
    u8g2.drawLine(cx - 4, cy + 9, cx, cy + 11); u8g2.drawLine(cx, cy + 11, cx + 4, cy + 9);  // smile
  }

  // mood extras (white)
  u8g2.setDrawColor(1);
  if (g_mood == HOT) {
    int sy = 2 + (int)((millis() / 150) % 8);
    u8g2.drawDisc(cx + 16, sy, 2);
    u8g2.drawLine(cx - 15, 4, cx - 12, 2); u8g2.drawLine(cx - 12, 2, cx - 9, 4);
  } else if (g_mood == COLD) {
    u8g2.drawLine(2, cy - 4, 6, cy - 6);  u8g2.drawLine(2, cy + 1, 6, cy - 1);
    u8g2.drawLine(40, cy - 4, 44, cy - 2); u8g2.drawLine(40, cy + 1, 44, cy + 3);
  }
}

// ---- 【6-5】依目前選的外觀 g_petSkin，決定要呼叫上面哪一個畫圖函式 ----
// 新增第 5 種寵物時：①加一個 drawPetXXX() ②在 PetSkin 列舉加名字 ③在這裡加一個 case
//                   ④3-4 的文字轉換、設定網頁下拉選單、儀表板選項也要同步加
void drawPet() {
  switch (g_petSkin) {
    case PET_FISH:  drawPetFish();  break;
    case PET_CAT:   drawPetCat();   break;
    case PET_PANDA: drawPetPanda(); break;
    default:        drawPetDrop();  break;
  }
}

// ============================================================================
// 七、資訊面板（螢幕右側）：由上到下畫 Wi-Fi 訊號、MQTT 狀態、時鐘、溫濕度、狀態文字
//     x = 51 是右側面板的左邊界；y 是文字底線的高度。
// ============================================================================
void drawStats() {
  const int x = 51;

  // ---- 【7-1】Wi-Fi 訊號格（0~3 格；實心 = 有訊號，空框 = 沒有）----
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

  // ---- 【7-2】MQTT 連線圓點（實心 = 已連上雲端，空心 = 沒連上）----
  // MQTT indicator dot (no label — room reserved for the clock)
  if (mqtt.connected()) u8g2.drawDisc(x + 15, 9, 3);
  else                  u8g2.drawCircle(x + 15, 9, 3);

  // ---- 【7-3】時鐘（台北時間，靠右對齊；還沒對到網路時間顯示 --:--）----
  // clock HH:MM (local), right-aligned with a 2 px margin
  char clk[8];
  localHHMM(clk, sizeof(clk));
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(126 - u8g2.getStrWidth(clk), 13, clk);

  // ---- 【7-4】溫度與濕度（還沒讀到就顯示 --.- 佔位）----
  // temperature / humidity
  char buf[24];
  u8g2.setFont(u8g2_font_7x14B_tr);
  if (!isnan(g_temp)) snprintf(buf, sizeof(buf), "%.1f C", g_temp);
  else                strcpy(buf, "--.- C");
  u8g2.drawStr(x, 30, buf);

  if (!isnan(g_hum)) snprintf(buf, sizeof(buf), "%.0f %%RH", g_hum);
  else               strcpy(buf, "-- %RH");
  u8g2.drawStr(x, 46, buf);

  // ---- 【7-5】最下方狀態文字：Hot! / Cold! / Comfy（依心情）----
  // status line
  u8g2.setFont(u8g2_font_5x8_tr);
  const char* s = (g_mood == HOT)  ? "Status: Hot!"
                : (g_mood == COLD) ? "Status: Cold!"
                                   : "Status: Comfy";
  u8g2.drawStr(x, 62, s);
}

// ============================================================================
// 八、週期工作 tick：每個 tickXXX() 開頭都先看「距離上次做超過多久了嗎？」，
//     還沒到就馬上 return，到了才真的做事。這樣主迴圈 loop() 可以一直轉、
//     不會被任何一件事卡住（整支程式完全不用 delay()）。
// ============================================================================

// ---- 【8-1】讀感測器：每 2 秒讀一次 DHT 溫濕度，讀失敗就沿用上一次的值，再更新心情 ----
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

// ---- 【8-2】設定模式畫面：設定入口開啟時，螢幕改顯示「要連哪個 Wi-Fi、密碼、網址」----
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

// ---- 【8-3】重畫螢幕：每 0.1 秒一次；設定模式畫 8-2，平常畫 6-5 寵物 + 7 資訊面板 ----
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

// ---- 【8-4】RGB 燈呼吸效果：綠=舒適 紅=太熱 藍=太冷 青色閃一下=剛送出資料 琥珀色=Wi-Fi 斷線 ----
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

// ---- 【8-5】組裝並送出一筆感測資料：打包成 JSON 文字，發布到 MQTT 的 telemetry 主題 ----
// 格式要和後端 worker/ingest.py 的【4-2】對得上：metrics=感測數值、net=網路狀態、pet=目前寵物
void publishTelemetry() {
  JsonDocument doc;
  doc["schema"]    = 1;
  doc["device_id"] = g_deviceId;
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

  doc["pet"]      = petSkinToString(g_petSkin);   // echo what's actually applied
  doc["pet_hot"]  = g_tempHot;
  doc["pet_cold"] = g_tempCold;

  char payload[384];
  size_t n = serializeJson(doc, payload, sizeof(payload));
  bool ok = mqtt.publish(TOPIC_TELEMETRY, (const uint8_t*)payload, n, false);
  Serial.printf("[MQTT] publish %s (%u B) -> %s\n", ok ? "OK" : "FAIL", (unsigned)n, TOPIC_TELEMETRY);
  g_lastPublishFlash = millis();
}

// ---- 【8-6】定時發布：每 60 秒呼叫一次 8-5（MQTT 沒連上就先不送）----
void tickPublish() {
  static uint32_t last = 0;
  if (millis() - last < PUBLISH_MS) return;
  if (!mqtt.connected()) return;
  last = millis();
  publishTelemetry();
}

// ============================================================================
// 九、連線維護：讓 Wi-Fi 和 MQTT 斷線後會自己重連，也負責接收雲端傳來的指令
//     （同樣是 tick 風格：不卡住、定時檢查）
// ============================================================================

// ---- 【9-1】Wi-Fi 維護：已連上就對時；斷線就試備用網路；斷超過 2 分鐘重開設定入口網頁 ----
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

// ---- 【9-2】收到雲端指令：儀表板發 {"pet":"cat"} 就換寵物、{"pet_hot":28} 就改門檻，立即生效並存進 NVS ----
// Remote pet-skin change: dashboard publishes a retained {"pet":"cat"} to
// TOPIC_CMD. Applied live (no reboot — drawPet() just reads g_petSkin every
// frame) and persisted to NVS so it also survives one.
void mqttCallback(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;   // malformed, ignore

  prefs.begin("aqua", false);
  if (!doc["pet"].isNull()) {
    g_petSkin = petSkinFromString(doc["pet"].as<String>());
    prefs.putString("pet", petSkinToString(g_petSkin));
    Serial.printf("[MQTT] pet skin -> %s\n", petSkinToString(g_petSkin));
  }
  if (!doc["pet_hot"].isNull()) {
    g_tempHot = doc["pet_hot"].as<float>();
    prefs.putFloat("thi", g_tempHot);
    Serial.printf("[MQTT] pet hot -> %.1f\n", g_tempHot);
  }
  if (!doc["pet_cold"].isNull()) {
    g_tempCold = doc["pet_cold"].as<float>();
    prefs.putFloat("tlo", g_tempCold);
    Serial.printf("[MQTT] pet cold -> %.1f\n", g_tempCold);
  }
  prefs.end();
}

// ---- 【9-3】MQTT 維護：已連上就處理收發；沒連上每 3 秒重連一次，連上後訂閱 cmd 主題 ----
void tickMqtt() {
  static uint32_t lastTry = 0;
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) { mqtt.loop(); return; }
  if (millis() - lastTry < MQTT_RETRY_MS) return;
  lastTry = millis();

  Serial.println("[MQTT] connecting...");
  // Last Will: retained "offline" on ungraceful disconnect
  bool ok = mqtt.connect(g_deviceId.c_str(), g_mqttUser.c_str(), g_mqttPass.c_str(),
                         TOPIC_STATUS, 1, true, "{\"online\":false}");
  if (ok) {
    mqtt.publish(TOPIC_STATUS, "{\"online\":true}", true);   // retained
    mqtt.subscribe(TOPIC_CMD);
    Serial.println("[MQTT] connected");
  } else {
    Serial.printf("[MQTT] failed, rc=%d\n", mqtt.state());
  }
}

// ============================================================================
// 十、程式入口：ESP32 通電後，Arduino 先執行一次 setup()，再不停重複執行 loop()
//     開機流程 (setup，只跑一次): 讀NVS設定 -> 組MQTT topic -> 初始化LED/OLED/
//     DHT -> 跑WiFiManager連線或開設定入口網站 -> 設定MQTT連線參數。
//     主迴圈 (loop，重複執行): 見【10-7】內 1~6 的步驟註解。
// ============================================================================

void setup() {
  Serial.begin(115200);
  delay(50);                         // one-time serial settle only (pre-loop)
  g_bootMs = millis();

  // ---- 【10-1】讀設定、組出三個 MQTT 主題（地址格式：aquaponics/站點/裝置ID/用途）----
  loadMqttConfig();                  // NVS -> g_mqtt* (falls back to secrets.h)

  snprintf(TOPIC_TELEMETRY, sizeof(TOPIC_TELEMETRY), "aquaponics/%s/%s/telemetry", SITE_ID, g_deviceId.c_str());
  snprintf(TOPIC_STATUS,    sizeof(TOPIC_STATUS),    "aquaponics/%s/%s/status",    SITE_ID, g_deviceId.c_str());
  snprintf(TOPIC_CMD,       sizeof(TOPIC_CMD),       "aquaponics/%s/%s/cmd",       SITE_ID, g_deviceId.c_str());

  // ---- 【10-2】初始化硬體：RGB 燈、OLED 螢幕（先顯示 booting...）、DHT 感測器 ----
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

  // ---- 【10-3】把目前設定預填進設定網頁的欄位（密碼欄刻意留空，留空 = 不變更）----
  // ---- Wi-Fi + MQTT provisioning (WiFiManager captive portal) ----
  pinMode(PIN_CONFIG_BTN, INPUT_PULLUP);

  {
    char portStr[6];
    snprintf(portStr, sizeof(portStr), "%u", g_mqttPort);
    p_devid.setValue(g_deviceId.c_str(), 40);
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
  // ---- 【10-4】設定入口網頁的組裝與啟動：加入欄位 → 設定外觀 → 開機後 3 秒內按住 BOOT 鍵可強制開啟 ----
  wm.addParameter(&p_devid);
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

  // ---- 【10-5】登記主/備用 Wi-Fi 給 wifiMulti（之後斷線時 9-1 才能切換網路）----
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

  // ---- 【10-6】設定 MQTT 連線參數（連線動作本身由 9-3 的 tickMqtt 負責）----
  // TLS：驗證伺服器憑證(見【1-8】)，不是只加密不驗證(setInsecure)了——
  // 如果連不上，先看序列埠有沒有印出 TLS/handshake 相關錯誤。
  netClient.setCACert(HIVEMQ_ROOT_CA);

  mqtt.setServer(g_mqttHost.c_str(), g_mqttPort);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);

  Serial.printf("[BOOT] setup done — device=%s · MQTT %s:%u user=%s\n",
                g_deviceId.c_str(), g_mqttHost.c_str(), g_mqttPort, g_mqttUser.c_str());
}

// ---- 【10-7】主迴圈：不停重複，每一圈依序做下面 6 件事（各 tick 自己判斷時間到了沒）----
void loop() {
  tickWiFi();          // 1. 維持/重連 WiFi，順便處理設定入口網站

  // Deferred post-portal restart: reboot once the newly-entered Wi-Fi has
  // actually connected (creds are now in NVS), or after a 15 s grace period
  // if it can't (e.g. wrong password) — either way the creds are persisted
  // and a fresh boot gives the cleanest re-init.
  if (g_cfgSaved && (WiFi.status() == WL_CONNECTED || millis() - g_cfgSavedAt > 15000UL)) {
    Serial.println("[CFG] applying new config — restarting now");
    delay(200);
    ESP.restart();
  }

  tickMqtt();           // 2. 維持/重連 MQTT，並收下雲端送來的 cmd 指令
  tickSensor();         // 3. 讀 DHT 感測器，更新目前溫濕度與寵物心情
  tickPublish();        // 4. 每隔 PUBLISH_MS 把感測資料發布到 MQTT
  tickDisplay();        // 5. 重繪 OLED(寵物動畫 + 數值)
  tickLed();            // 6. 更新 RGB LED 呼吸燈效果
  // no delay(): loop spins freely, each tick self-throttles on millis()
}
