/**
 * wm_strings_zh_TW.h
 * Traditional Chinese strings for WiFiManager (tzapu), captive-portal
 * configuration UI.
 *
 * Activated via platformio.ini build flag:
 *   -D WM_STRINGS_FILE='"wm_strings_zh_TW.h"'
 * (WiFiManager.h does `#include WM_STRINGS_FILE`, defaulting to its own
 * wm_strings_en.h; this file is a drop-in replacement with the same symbol
 * names, just translated. wm_consts_en.h holds internal routing tokens only
 * — no visible text — so it's reused unchanged.)
 *
 * Based on tzapu/WiFiManager v2.0.17's wm_strings_en.h.
 */

#ifndef _WM_STRINGS_ZH_TW_H_
#define _WM_STRINGS_ZH_TW_H_

// strings files must include a consts file!
#include "wm_consts_en.h" // include constants, tokens, routes (not user-visible, no translation needed)

const char WM_LANGUAGE[] PROGMEM = "zh-TW"; // i18n lang code

const char HTTP_HEAD_START[]       PROGMEM = "<!DOCTYPE html>"
"<html lang='zh-Hant'><head>"
"<meta name='format-detection' content='telephone=no'>"
"<meta charset='UTF-8'>"
"<meta  name='viewport' content='width=device-width,initial-scale=1,user-scalable=no'/>"
"<title>{v}</title>";

const char HTTP_SCRIPT[]           PROGMEM = "<script>function c(l){"
"document.getElementById('s').value=l.getAttribute('data-ssid')||l.innerText||l.textContent;"
"p = l.nextElementSibling.classList.contains('l');"
"document.getElementById('p').disabled = !p;"
"if(p)document.getElementById('p').focus();};"
"function f() {var x = document.getElementById('p');x.type==='password'?x.type='text':x.type='password';}"
"</script>";

const char HTTP_HEAD_END[]         PROGMEM = "</head><body class='{c}'><div class='wrap'>";
const char HTTP_ROOT_MAIN[]        PROGMEM = "<h1>{t}</h1><h3>{v}</h3>";

const char * const HTTP_PORTAL_MENU[] PROGMEM = {
"<form action='/wifi'    method='get'><button>設定 Wi-Fi</button></form><br/>\n", // MENU_WIFI
"<form action='/0wifi'   method='get'><button>設定 Wi-Fi(不掃描)</button></form><br/>\n", // MENU_WIFINOSCAN
"<form action='/info'    method='get'><button>系統資訊</button></form><br/>\n", // MENU_INFO
"<form action='/param'   method='get'><button>系統設定</button></form><br/>\n",//MENU_PARAM
"<form action='/close'   method='get'><button>關閉</button></form><br/>\n", // MENU_CLOSE
"<form action='/restart' method='get'><button>重新啟動</button></form><br/>\n",// MENU_RESTART
"<form action='/exit'    method='get'><button>退出</button></form><br/>\n",  // MENU_EXIT
"<form action='/erase'   method='get'><button class='D'>清除設定</button></form><br/>\n", // MENU_ERASE
"<form action='/update'  method='get'><button>更新韌體</button></form><br/>\n",// MENU_UPDATE
"<hr><br/>" // MENU_SEP
};

const char HTTP_PORTAL_OPTIONS[]   PROGMEM = "";
const char HTTP_ITEM_QI[]          PROGMEM = "<div role='img' aria-label='{r}%' title='{r}%' class='q q-{q} {i} {h}'></div>";
const char HTTP_ITEM_QP[]          PROGMEM = "<div class='q {h}'>{r}%</div>";
const char HTTP_ITEM[]             PROGMEM = "<div><a href='#p' onclick='c(this)' data-ssid='{V}'>{v}</a>{qi}{qp}</div>";

const char HTTP_FORM_START[]       PROGMEM = "<form method='POST' action='{v}'>";
const char HTTP_FORM_WIFI[]        PROGMEM = "<label for='s'>Wi-Fi 名稱(SSID)</label><input id='s' name='s' maxlength='32' autocorrect='off' autocapitalize='none' placeholder='{v}'><br/><label for='p'>密碼</label><input id='p' name='p' maxlength='64' type='password' placeholder='{p}'><input type='checkbox' id='showpass' onclick='f()'> <label for='showpass'>顯示密碼</label><br/>";
const char HTTP_FORM_WIFI_END[]    PROGMEM = "";
const char HTTP_FORM_STATIC_HEAD[] PROGMEM = "<hr><br/>";
const char HTTP_FORM_END[]         PROGMEM = "<br/><br/><button type='submit'>儲存</button></form>";
const char HTTP_FORM_LABEL[]       PROGMEM = "<label for='{i}'>{t}</label>";
const char HTTP_FORM_PARAM_HEAD[]  PROGMEM = "<hr><br/>";
const char HTTP_FORM_PARAM[]       PROGMEM = "<br/><input id='{i}' name='{n}' maxlength='{l}' value='{v}' {c}>\n"; // do not remove newline!

const char HTTP_SCAN_LINK[]        PROGMEM = "<br/><form action='/wifi?refresh=1' method='POST'><button name='refresh' value='1'>重新掃描</button></form>";
const char HTTP_SAVED[]            PROGMEM = "<div class='msg'>正在儲存設定<br/>ESP 正嘗試連線到網路。<br />若連線失敗，請重新連上此 AP 再試一次</div>";
const char HTTP_PARAMSAVED[]       PROGMEM = "<div class='msg S'>已儲存<br/></div>";
const char HTTP_END[]              PROGMEM = "</div></body></html>";
const char HTTP_ERASEBTN[]         PROGMEM = "<br/><form action='/erase' method='get'><button class='D'>清除 Wi-Fi 設定</button></form>";
const char HTTP_UPDATEBTN[]        PROGMEM = "<br/><form action='/update' method='get'><button>更新韌體</button></form>";
const char HTTP_BACKBTN[]          PROGMEM = "<hr><br/><form action='/' method='get'><button>返回</button></form>";

const char HTTP_STATUS_ON[]        PROGMEM = "<div class='msg S'><strong>已連線</strong>到 {v}<br/><em><small>IP 位址 {i}</small></em></div>";
const char HTTP_STATUS_OFF[]       PROGMEM = "<div class='msg {c}'><strong>未連線</strong>到 {v}{r}</div>";
const char HTTP_STATUS_OFFPW[]     PROGMEM = "<br/>密碼驗證失敗";
const char HTTP_STATUS_OFFNOAP[]   PROGMEM = "<br/>找不到此無線基地台";
const char HTTP_STATUS_OFFFAIL[]   PROGMEM = "<br/>無法連線";
const char HTTP_STATUS_NONE[]      PROGMEM = "<div class='msg'>尚未設定任何 Wi-Fi</div>";
const char HTTP_BR[]               PROGMEM = "<br/>";

const char HTTP_STYLE[]            PROGMEM = "<style>"
".c,body{text-align:center;font-family:verdana}div,input,select{padding:5px;font-size:1em;margin:5px 0;box-sizing:border-box}"
"input,button,select,.msg{border-radius:.3rem;width: 100%}input[type=radio],input[type=checkbox]{width:auto}"
"button,input[type='button'],input[type='submit']{cursor:pointer;border:0;background-color:#1fa3ec;color:#fff;line-height:2.4rem;font-size:1.2rem;width:100%}"
"input[type='file']{border:1px solid #1fa3ec}"
".wrap {text-align:left;display:inline-block;min-width:260px;max-width:500px}"
"a{color:#000;font-weight:700;text-decoration:none}a:hover{color:#1fa3ec;text-decoration:underline}"
".q{height:16px;margin:0;padding:0 5px;text-align:right;min-width:38px;float:right}.q.q-0:after{background-position-x:0}.q.q-1:after{background-position-x:-16px}.q.q-2:after{background-position-x:-32px}.q.q-3:after{background-position-x:-48px}.q.q-4:after{background-position-x:-64px}.q.l:before{background-position-x:-80px;padding-right:5px}.ql .q{float:left}.q:after,.q:before{content:'';width:16px;height:16px;display:inline-block;background-repeat:no-repeat;background-position: 16px 0;"
"background-image:url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAAAQCAMAAADeZIrLAAAAJFBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADHJj5lAAAAC3RSTlMAIjN3iJmqu8zd7vF8pzcAAABsSURBVHja7Y1BCsAwCASNSVo3/v+/BUEiXnIoXkoX5jAQMxTHzK9cVSnvDxwD8bFx8PhZ9q8FmghXBhqA1faxk92PsxvRc2CCCFdhQCbRkLoAQ3q/wWUBqG35ZxtVzW4Ed6LngPyBU2CobdIDQ5oPWI5nCUwAAAAASUVORK5CYII=');}"
"@media (-webkit-min-device-pixel-ratio: 2),(min-resolution: 192dpi){.q:before,.q:after {"
"background-image:url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALwAAAAgCAMAAACfM+KhAAAALVBMVEX///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAOrOgAAAADnRSTlMAESIzRGZ3iJmqu8zd7gKjCLQAAACmSURBVHgB7dDBCoMwEEXRmKlVY3L//3NLhyzqIqSUggy8uxnhCR5Mo8xLt+14aZ7wwgsvvPA/ofv9+44334UXXngvb6XsFhO/VoC2RsSv9J7x8BnYLW+AjT56ud/uePMdb7IP8Bsc/e7h8Cfk912ghsNXWPpDC4hvN+D1560A1QPORyh84VKLjjdvfPFm++i9EWq0348XXnjhhT+4dIbCW+WjZim9AKk4UZMnnCEuAAAAAElFTkSuQmCC');"
"background-size: 95px 16px;}}"
".msg{padding:20px;margin:20px 0;border:1px solid #eee;border-left-width:5px;border-left-color:#777}.msg h4{margin-top:0;margin-bottom:5px}.msg.P{border-left-color:#1fa3ec}.msg.P h4{color:#1fa3ec}.msg.D{border-left-color:#dc3630}.msg.D h4{color:#dc3630}.msg.S{border-left-color: #5cb85c}.msg.S h4{color: #5cb85c}"
"dt{font-weight:bold}dd{margin:0;padding:0 0 0.5em 0;min-height:12px}"
"td{vertical-align: top;}"
".h{display:none}"
"button{transition: 0s opacity;transition-delay: 3s;transition-duration: 0s;cursor: pointer}"
"button.D{background-color:#dc3630}"
"button:active{opacity:50% !important;cursor:wait;transition-delay: 0s}"
"body.invert,body.invert a,body.invert h1 {background-color:#060606;color:#fff;}"
"body.invert .msg{color:#fff;background-color:#282828;border-top:1px solid #555;border-right:1px solid #555;border-bottom:1px solid #555;}"
"body.invert .q[role=img]{-webkit-filter:invert(1);filter:invert(1);}"
":disabled {opacity: 0.5;}"
"</style>";

#ifndef WM_NOHELP
const char HTTP_HELP[]             PROGMEM =
 "<br/><h3>可用頁面</h3><hr>"
 "<table class='table'>"
 "<thead><tr><th>頁面</th><th>功能</th></tr></thead><tbody>"
 "<tr><td><a href='/'>/</a></td>"
 "<td>選單頁面。</td></tr>"
 "<tr><td><a href='/wifi'>/wifi</a></td>"
 "<td>顯示 Wi-Fi 掃描結果並輸入 Wi-Fi 設定。(/0wifi 不掃描)</td></tr>"
 "<tr><td><a href='/wifisave'>/wifisave</a></td>"
 "<td>儲存 Wi-Fi 設定並套用到裝置，需要提供對應參數。</td></tr>"
 "<tr><td><a href='/param'>/param</a></td>"
 "<td>參數頁面</td></tr>"
 "<tr><td><a href='/info'>/info</a></td>"
 "<td>系統資訊頁面</td></tr>"
 "<tr><td><a href='/u'>/u</a></td>"
 "<td>OTA 更新</td></tr>"
 "<tr><td><a href='/close'>/close</a></td>"
 "<td>關閉入口彈出視窗，設定入口仍會保持運作</td></tr>"
 "<tr><td>/exit</td>"
 "<td>退出設定入口，設定入口將會關閉</td></tr>"
 "<tr><td>/restart</td>"
 "<td>重新啟動裝置</td></tr>"
 "<tr><td>/erase</td>"
 "<td>清除 Wi-Fi 設定並重新啟動裝置。清除後裝置將不會自動連線，直到輸入新的 Wi-Fi 設定為止。</td></tr>"
 "</table>"
 "<p/>Github <a href='https://github.com/tzapu/WiFiManager'>https://github.com/tzapu/WiFiManager</a>.";
#else
const char HTTP_HELP[]             PROGMEM = "";
#endif

const char HTTP_UPDATE[] PROGMEM = "上傳新韌體<br/><form method='POST' action='u' enctype='multipart/form-data' onchange=\"(function(el){document.getElementById('uploadbin').style.display = el.value=='' ? 'none' : 'initial';})(this)\"><input type='file' name='update' accept='.bin,application/octet-stream'><button id='uploadbin' type='submit' class='h D'>更新</button></form><small><a href='http://192.168.4.1/update' target='_blank'>* 在強制門戶內可能無法運作，請另開瀏覽器連到 http://192.168.4.1</a><small>";
const char HTTP_UPDATE_FAIL[] PROGMEM = "<div class='msg D'><strong>更新失敗！</strong><Br/>請重新啟動裝置後再試一次</div>";
const char HTTP_UPDATE_SUCCESS[] PROGMEM = "<div class='msg S'><strong>更新成功。</strong> <br/> 裝置正在重新啟動…</div>";

#ifdef WM_JSTEST
const char HTTP_JS[] PROGMEM =
"<script>function postAjax(url, data, success) {"
"    var params = typeof data == 'string' ? data : Object.keys(data).map("
"            function(k){ return encodeURIComponent(k) + '=' + encodeURIComponent(data[k]) }"
"        ).join('&');"
"    var xhr = window.XMLHttpRequest ? new XMLHttpRequest() : new ActiveXObject(\"Microsoft.XMLHTTP\");"
"    xhr.open('POST', url);"
"    xhr.onreadystatechange = function() {"
"        if (xhr.readyState>3 && xhr.status==200) { success(xhr.responseText); }"
"    };"
"    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');"
"    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');"
"    xhr.send(params);"
"    return xhr;}"
"postAjax('/status', 'p1=1&p2=Hello+World', function(data){ console.log(data); });"
"postAjax('/status', { p1: 1, p2: 'Hello World' }, function(data){ console.log(data); });"
"</script>";
#endif

// Info html
#ifdef ESP32
	const char HTTP_INFO_esphead[]    PROGMEM = "<h3>ESP32</h3><hr><dl>";
	const char HTTP_INFO_chiprev[]    PROGMEM = "<dt>晶片版本</dt><dd>{1}</dd>";
  	const char HTTP_INFO_lastreset[]  PROGMEM = "<dt>上次重置原因</dt><dd>CPU0: {1}<br/>CPU1: {2}</dd>";
  	const char HTTP_INFO_aphost[]     PROGMEM = "<dt>熱點主機名稱</dt><dd>{1}</dd>";
    const char HTTP_INFO_psrsize[]    PROGMEM = "<dt>PSRAM 容量</dt><dd>{1} bytes</dd>";
	const char HTTP_INFO_temp[]       PROGMEM = "<dt>溫度</dt><dd>{1} C&deg; / {2} F&deg;</dd>";
    const char HTTP_INFO_hall[]       PROGMEM = "<dt>霍爾感測器</dt><dd>{1}</dd>";
#else
	const char HTTP_INFO_esphead[]    PROGMEM = "<h3>ESP8266</h3><hr><dl>";
	const char HTTP_INFO_fchipid[]    PROGMEM = "<dt>Flash 晶片 ID</dt><dd>{1}</dd>";
	const char HTTP_INFO_corever[]    PROGMEM = "<dt>核心版本</dt><dd>{1}</dd>";
	const char HTTP_INFO_bootver[]    PROGMEM = "<dt>Boot 版本</dt><dd>{1}</dd>";
	const char HTTP_INFO_lastreset[]  PROGMEM = "<dt>上次重置原因</dt><dd>{1}</dd>";
	const char HTTP_INFO_flashsize[]  PROGMEM = "<dt>實際 Flash 容量</dt><dd>{1} bytes</dd>";
#endif

const char HTTP_INFO_memsmeter[]  PROGMEM = "<br/><progress value='{1}' max='{2}'></progress></dd>";
const char HTTP_INFO_memsketch[]  PROGMEM = "<dt>記憶體 - 程式大小</dt><dd>已使用 / 總計 bytes<br/>{1} / {2}";
const char HTTP_INFO_freeheap[]   PROGMEM = "<dt>記憶體 - 可用 Heap</dt><dd>可用 {1} bytes</dd>";
const char HTTP_INFO_wifihead[]   PROGMEM = "<br/><h3>Wi-Fi</h3><hr>";
const char HTTP_INFO_uptime[]     PROGMEM = "<dt>已執行時間</dt><dd>{1} 分 {2} 秒</dd>";
const char HTTP_INFO_chipid[]     PROGMEM = "<dt>晶片 ID</dt><dd>{1}</dd>";
const char HTTP_INFO_idesize[]    PROGMEM = "<dt>Flash 容量</dt><dd>{1} bytes</dd>";
const char HTTP_INFO_sdkver[]     PROGMEM = "<dt>SDK 版本</dt><dd>{1}</dd>";
const char HTTP_INFO_cpufreq[]    PROGMEM = "<dt>CPU 頻率</dt><dd>{1}MHz</dd>";
const char HTTP_INFO_apip[]       PROGMEM = "<dt>熱點 IP</dt><dd>{1}</dd>";
const char HTTP_INFO_apmac[]      PROGMEM = "<dt>熱點 MAC</dt><dd>{1}</dd>";
const char HTTP_INFO_apssid[]     PROGMEM = "<dt>熱點名稱(SSID)</dt><dd>{1}</dd>";
const char HTTP_INFO_apbssid[]    PROGMEM = "<dt>BSSID</dt><dd>{1}</dd>";
const char HTTP_INFO_stassid[]    PROGMEM = "<dt>連線中的 Wi-Fi(SSID)</dt><dd>{1}</dd>";
const char HTTP_INFO_staip[]      PROGMEM = "<dt>裝置 IP</dt><dd>{1}</dd>";
const char HTTP_INFO_stagw[]      PROGMEM = "<dt>閘道器</dt><dd>{1}</dd>";
const char HTTP_INFO_stasub[]     PROGMEM = "<dt>子網遮罩</dt><dd>{1}</dd>";
const char HTTP_INFO_dnss[]       PROGMEM = "<dt>DNS 伺服器</dt><dd>{1}</dd>";
const char HTTP_INFO_host[]       PROGMEM = "<dt>主機名稱</dt><dd>{1}</dd>";
const char HTTP_INFO_stamac[]     PROGMEM = "<dt>裝置 MAC</dt><dd>{1}</dd>";
const char HTTP_INFO_conx[]       PROGMEM = "<dt>已連線</dt><dd>{1}</dd>";
const char HTTP_INFO_autoconx[]   PROGMEM = "<dt>自動連線</dt><dd>{1}</dd>";

const char HTTP_INFO_aboutver[]     PROGMEM = "<dt>WiFiManager</dt><dd>{1}</dd>";
const char HTTP_INFO_aboutarduino[] PROGMEM = "<dt>Arduino</dt><dd>{1}</dd>";
const char HTTP_INFO_aboutsdk[]     PROGMEM = "<dt>ESP-SDK/IDF</dt><dd>{1}</dd>";
const char HTTP_INFO_aboutdate[]    PROGMEM = "<dt>編譯日期</dt><dd>{1}</dd>";

const char S_brand[]              PROGMEM = "WiFiManager";
const char S_debugPrefix[]        PROGMEM = "*wm:";
const char S_y[]                  PROGMEM = "是";
const char S_n[]                  PROGMEM = "否";
const char S_enable[]             PROGMEM = "已啟用";
const char S_disable[]            PROGMEM = "已停用";
const char S_GET[]                PROGMEM = "GET";
const char S_POST[]               PROGMEM = "POST";
const char S_NA[]                 PROGMEM = "未知";
const char S_passph[]             PROGMEM = "********";
const char S_titlewifisaved[]     PROGMEM = "帳密已儲存";
const char S_titlewifisettings[]  PROGMEM = "設定已儲存";
const char S_titlewifi[]          PROGMEM = "設定 ESP32";
const char S_titleinfo[]          PROGMEM = "系統資訊";
const char S_titleparam[]         PROGMEM = "系統設定";
const char S_titleparamsaved[]    PROGMEM = "設定已儲存";
const char S_titleexit[]          PROGMEM = "退出";
const char S_titlereset[]         PROGMEM = "重置";
const char S_titleerase[]         PROGMEM = "清除設定";
const char S_titleclose[]         PROGMEM = "關閉";
const char S_options[]            PROGMEM = "選項";
const char S_nonetworks[]         PROGMEM = "找不到任何 Wi-Fi 訊號，請重新掃描。";
const char S_staticip[]           PROGMEM = "固定 IP";
const char S_staticgw[]           PROGMEM = "固定閘道器";
const char S_staticdns[]          PROGMEM = "固定 DNS";
const char S_subnet[]             PROGMEM = "子網遮罩";
const char S_exiting[]            PROGMEM = "正在退出";
const char S_resetting[]          PROGMEM = "裝置將在幾秒後重新啟動。";
const char S_closing[]            PROGMEM = "您可以關閉此頁面，設定入口仍會持續運作";
const char S_error[]              PROGMEM = "發生錯誤";
const char S_notfound[]           PROGMEM = "找不到檔案\n\n";
const char S_uri[]                PROGMEM = "URI: ";
const char S_method[]             PROGMEM = "\nMethod: ";
const char S_args[]               PROGMEM = "\nArguments: ";
const char S_parampre[]           PROGMEM = "param_";

// debug strings
const char D_HR[]                 PROGMEM = "--------------------";

// softap ssid default prefix
#ifdef ESP8266
    const char S_ssidpre[]        PROGMEM = "ESP";
#elif defined(ESP32)
    const char S_ssidpre[]        PROGMEM = "ESP32";
#else
    const char S_ssidpre[]        PROGMEM = "WM";
#endif

#endif
