"""
╔═══════════════════════════════════ 檔案導覽 ═══════════════════════════════════╗

類別：測試（Tests）── 用 pytest 寫的自動化測試，每次推送到 GitHub，CI 都會自動跑一遍。
目的：確認後端 worker/ingest.py 的功能沒有被改壞，共驗證 4 件事：
      1. 收到的 MQTT 封包，有被正確拆成資料庫欄位
      2. LINE 警戒訊息的文字、以及「要不要推播」的判斷正確（含暫停開關）
      3. 各種意外（壞 JSON、資料庫掛掉、網路錯誤）不會讓程式當掉
      4. AI 預測排程會定時執行，而且出錯不會拖垮收資料
運作方式：不連真正的資料庫和 LINE，用「假資料庫」(tests/conftest.py 的 FakeClient)
          和「假函式」代替，所以幾秒就跑完，也不會動到真實資料。
被測程式：worker/ingest.py

程式架構（每個測試前面的【編號】後面，標了它在測 ingest.py 的哪一項）：
  一、測試前的準備      【1-1】假的 MQTT 封包產生器
  二、時間轉換          【2-1】UTC 轉台北時間  【2-2】跨午夜  【2-3】格式錯誤時原樣回傳
  三、處理一筆封包      【3-1】拆欄位  【3-2】沒時間不帶 ts  【3-3】沒裝置名改用主題
                        【3-4】壞 JSON 略過  【3-5】資料庫出錯不當機
  四、LINE 警戒訊息     【4-1】測試用的警戒資料  【4-2】高於上限  【4-3】低於下限
                        【4-4】單一原子更新  【4-5】沒權杖就不動作
  五、LINE 暫停與送出   【5-1】讀全域暫停  【5-2】裝置強制暫停  【5-3】裝置強制開啟
                        【5-4】裝置檢查失敗改看全域  【5-5】資料庫全壞時不暫停
                        【5-6】暫停時不送出  【5-7】帶權杖送出廣播  【5-8】網路錯誤不當機
  六、AI 預測排程       【6-1】跑一次呼叫 baseline  【6-2】出錯不拖垮收資料  【6-3】跑完睡 30 分鐘再跑

備註：
  想確認某功能有沒有被測到 ... 看「程式架構」，找到對應的項目
  新增一個測試 ............. 放在對應的大標題下面，函式名稱用 test_ 開頭
  改了 ingest.py 某一段 ..... 先找那段的編號，再找這裡對應的測試，改完重跑
  假資料庫怎麼運作 ......... tests/conftest.py 的 FakeClient、FakeQuery
  名詞：assert = 檢查「結果一定要是這樣」，不是就算測試失敗
        monkeypatch = 測試期間暫時把某個東西換成假的，測完自動還原
        每個測試都是三步：①準備(換成假的) ②執行(呼叫被測函式) ③檢查(assert)

╚════════════════════════════════════════════════════════════════════════════╝
"""
import json

import ingest
import pytest

# ============================================================================
# 一、測試前的準備
# ============================================================================

# ---- 【1-1】假的 MQTT 封包產生器：模擬 ESP32 送來的資料，要改哪個欄位就傳進來覆蓋 ----
def _payload(**over):
    p = {
        "device_id": "esp32-aqua-01",
        "site_id": "home",
        "fw": "1.2.0",
        "ts": "2026-09-16T03:12:00Z",
        "metrics": {"temperature": 31.9, "humidity": 55, "foo": 1},
        "net": {"rssi": -60, "ssid": "x"},
        "pet": "cat",
        "pet_hot": 28,
        "pet_cold": 18,
    }
    p.update(over)
    return json.dumps(p).encode()


# ============================================================================
# 二、時間轉換：測 ingest.py 第二項的 2-2（UTC 轉台北時間，給 LINE 訊息用）
# ============================================================================

# ---- 【2-1】UTC 時間要加 8 小時變成台北時間 ----
def test_fmt_taipei_converts_utc_to_plus_8():
    assert ingest._fmt_taipei("2026-09-16T03:12:00Z") == "2026-09-16 11:12"


# ---- 【2-2】加 8 小時後跨過午夜，日期要跟著進一天 ----
def test_fmt_taipei_crosses_midnight():
    assert ingest._fmt_taipei("2026-09-16T20:30:00+00:00") == "2026-09-17 04:30"


# ---- 【2-3】不是時間格式的輸入，不報錯、原樣轉成文字回傳 ----
def test_fmt_taipei_falls_back_to_raw_string():
    assert ingest._fmt_taipei("not a date") == "not a date"
    assert ingest._fmt_taipei(None) == "None"


# ============================================================================
# 三、處理一筆封包：測 ingest.py 第四項的 handle_payload()（解析 → 整理 → 寫入資料庫）
# ============================================================================

# ---- 【3-1】有專屬欄位的數值放進各自欄位，其餘放進 extra；裝置狀態與遙測各寫一筆（對應 ingest.py 4-2、4-3）----
def test_handle_payload_splits_known_columns_and_extra(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", lambda _d: None)

    ingest.handle_payload("aquaponics/home/esp32-aqua-01/telemetry", _payload())

    device_q, telemetry_q = client.queries
    assert device_q.table == "aqua_devices"
    dev = device_q.op("upsert")[0][0]
    assert (dev["device_id"], dev["site_id"], dev["fw_version"]) == ("esp32-aqua-01", "home", "1.2.0")
    assert (dev["pet_skin"], dev["pet_hot"], dev["pet_cold"]) == ("cat", 28, 18)

    row = telemetry_q.op("insert")[0][0]
    assert row["temperature"] == 31.9 and row["humidity"] == 55
    assert row["rssi"] == -60 and row["ts"] == "2026-09-16T03:12:00Z"
    # unknown metric + non-rssi net fields + envelope fields go to `extra`
    assert row["extra"] == {"foo": 1, "ssid": "x", "fw": "1.2.0", "site_id": "home"}


# ---- 【3-2】裝置還沒對到網路時間（沒有 ts）時，不要自己填，交給資料庫用「現在時間」（對應 ingest.py 4-2）----
def test_handle_payload_omits_ts_when_device_clock_unsynced(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", lambda _d: None)

    ingest.handle_payload("aquaponics/home/esp32-aqua-01/telemetry", _payload(ts=None))

    assert "ts" not in client.queries[1].op("insert")[0][0]  # DB default now() applies


# ---- 【3-3】封包裡沒有裝置名稱時，改用 MQTT 主題的第 3 段（對應 ingest.py 4-2）----
def test_handle_payload_device_id_falls_back_to_topic(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", lambda _d: None)

    ingest.handle_payload("aquaponics/home/board-7/telemetry", _payload(device_id=None))

    assert client.queries[1].op("insert")[0][0]["device_id"] == "board-7"


# ---- 【3-4】JSON 格式壞掉的封包直接略過，完全不碰資料庫（對應 ingest.py 4-1）----
def test_handle_payload_ignores_bad_json(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)

    ingest.handle_payload("aquaponics/home/x/telemetry", b"{not json")

    assert client.queries == []


# ---- 【3-5】資料庫寫入失敗時不當機，也不去推 LINE（對應 ingest.py 4-3、4-4）----
def test_handle_payload_db_error_does_not_raise_or_notify(monkeypatch):
    class Boom:
        def table(self, _name):
            raise RuntimeError("db down")

    called = []
    monkeypatch.setattr(ingest, "sb", Boom())
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", called.append)

    ingest.handle_payload("aquaponics/home/x/telemetry", _payload())

    assert called == []


# ============================================================================
# 四、LINE 警戒訊息：測 ingest.py 第三項的 3-3（領取未推播警戒、組成訊息）
# ============================================================================

# ---- 【4-1】測試用的輔助工具：_alert 造一筆警戒資料；_run_notify 用假資料庫與假推播執行一次，並收集推出的訊息 ----
def _alert(value, lo=22, hi=31.7, metric="temperature"):
    return {"ts": "2026-09-16T03:12:00Z", "metric": metric, "value": value,
            "min_val": lo, "max_val": hi}


def _run_notify(monkeypatch, fake_client, rows):
    client = fake_client(lambda q: rows)
    pushed = []
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "push_line", lambda text, device_id: pushed.append(text))
    ingest.notify_pending_threshold_alerts("esp32-aqua-01")
    return client, pushed


# ---- 【4-2】數值高於上限：訊息要寫「高於上限」，並帶時間（台北）、裝置、項目、數值 ----
def test_alert_message_above_upper_bound(monkeypatch, fake_client):
    _, pushed = _run_notify(monkeypatch, fake_client, [_alert(31.9)])
    assert pushed == [
        "⚠️ AIoT系統警戒\n"
        "時間：2026-09-16 11:12\n"
        "裝置：esp32-aqua-01\n"
        "項目：氣溫\n"
        "數值：31.9 °C\n"
        "判讀：高於上限，正常 22–31.7"
    ]


# ---- 【4-3】數值低於下限：訊息要寫「低於下限」，濕度單位是 %RH ----
def test_alert_message_below_lower_bound(monkeypatch, fake_client):
    _, pushed = _run_notify(monkeypatch, fake_client, [_alert(35, lo=40, hi=60, metric="humidity")])
    assert "項目：濕度" in pushed[0]
    assert "數值：35 %RH" in pushed[0]
    assert "判讀：低於下限，正常 40–60" in pushed[0]


# ---- 【4-4】領取警戒必須是「一次更新」，不能先查再改，否則兩個程式同時跑會重複推播同一則 ----
def test_alert_claim_is_a_single_atomic_update(monkeypatch, fake_client):
    client, _ = _run_notify(monkeypatch, fake_client, [_alert(31.9)])
    (q,) = client.queries  # one round trip: no select-then-update race
    assert q.table == "aqua_anomalies"
    assert q.has("update") and not q.has("select")
    assert q.op("is_")[0] == ("notified_at", "null")


# ---- 【4-5】沒設定 LINE 權杖時整個功能關閉，連資料庫都不查 ----
def test_notify_is_noop_without_line_token(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "")
    ingest.notify_pending_threshold_alerts("esp32-aqua-01")
    assert client.queries == []


# ============================================================================
# 五、LINE 暫停與送出：測 ingest.py 第三項的 3-1（是否暫停）與 3-2（送出訊息）
#     暫停規則：先看「該裝置自己的設定」，沒設才看「全域開關」
# ============================================================================

# ---- 【5-1】裝置沒有個別設定時，採用全域開關的值（對應 ingest.py 3-1）----
def test_line_push_paused_reads_setting(monkeypatch, fake_client):
    def handler(q):
        if q.table == "aqua_devices":
            return [{"line_push_paused": None}]  # no per-device override
        return [{"value": True}]

    monkeypatch.setattr(ingest, "sb", fake_client(handler))
    assert ingest.line_push_paused("esp32-aqua-01") is True


# ---- 【5-2】裝置個別設定「暫停」時，就算全域沒暫停也要暫停（個別設定優先；對應 ingest.py 3-1）----
def test_line_push_paused_device_override_forces_pause(monkeypatch, fake_client):
    def handler(q):
        if q.table == "aqua_devices":
            return [{"line_push_paused": True}]
        return [{"value": False}]  # global says not paused; override wins

    monkeypatch.setattr(ingest, "sb", fake_client(handler))
    assert ingest.line_push_paused("esp32-aqua-01") is True


# ---- 【5-3】裝置個別設定「不暫停」時，就算全域已暫停，這台仍要推播（對應 ingest.py 3-1）----
def test_line_push_paused_device_override_forces_on(monkeypatch, fake_client):
    def handler(q):
        if q.table == "aqua_devices":
            return [{"line_push_paused": False}]
        return [{"value": True}]  # global paused; override forces this device on

    monkeypatch.setattr(ingest, "sb", fake_client(handler))
    assert ingest.line_push_paused("esp32-aqua-01") is False


# ---- 【5-4】讀「裝置個別設定」失敗（例如資料庫還沒新增那個欄位）時，改看全域開關，不能讓全域開關失效 ----
def test_line_push_paused_falls_back_to_global_when_device_check_fails(monkeypatch, fake_client):
    # e.g. production hasn't run sql/08_notify_pause_device.sql yet, so
    # aqua_devices.line_push_paused doesn't exist — must not make the
    # user's global on/off switch stop applying.
    def handler(q):
        if q.table == "aqua_devices":
            raise Exception('column "line_push_paused" does not exist (42703)')
        return [{"value": True}]

    monkeypatch.setattr(ingest, "sb", fake_client(handler))
    assert ingest.line_push_paused("esp32-aqua-01") is True


# ---- 【5-5】兩種設定都讀不到時，視為「沒暫停」，寧可多推也不要漏掉真正的警戒 ----
def test_line_push_paused_fails_open_on_db_error(monkeypatch):
    class Boom:
        def table(self, _name):
            raise RuntimeError("db down")

    monkeypatch.setattr(ingest, "sb", Boom())
    assert ingest.line_push_paused("esp32-aqua-01") is False  # never swallow a real alert


# ---- 【5-6】被暫停時不呼叫 LINE（對應 ingest.py 3-2）----
def test_push_line_skipped_when_paused(monkeypatch):
    posts = []
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda _device_id: True)
    monkeypatch.setattr(ingest.requests, "post", lambda *a, **k: posts.append(a))
    ingest.push_line("hi", "esp32-aqua-01")
    assert posts == []


# ---- 【5-7】正常送出：對 LINE 廣播網址發出請求，帶上權杖與訊息內容（對應 ingest.py 3-2）----
def test_push_line_posts_broadcast_with_bearer_token(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda _device_id: False)
    monkeypatch.setattr(ingest.requests, "post", lambda url, **k: calls.append((url, k)) or Resp())

    ingest.push_line("hello", "esp32-aqua-01")

    (url, kw), = calls
    assert url == ingest.LINE_BROADCAST_URL
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["json"] == {"messages": [{"type": "text", "text": "hello"}]}


# ---- 【5-8】網路出錯（連不上 LINE）時不當機，收資料的主流程要繼續跑（對應 ingest.py 3-2）----
def test_push_line_swallows_network_errors(monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError("offline")

    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda _device_id: False)
    monkeypatch.setattr(ingest.requests, "post", boom)
    ingest.push_line("hello", "esp32-aqua-01")  # must not raise — ingest keeps running


# ============================================================================
# 六、AI 預測排程：測 ingest.py 第六項（每 30 分鐘呼叫 analysis/baseline.py）
# ============================================================================

# ---- 【6-1】跑一次預測，就是呼叫一次 baseline.main()（對應 ingest.py 6-1）----
def test_run_forecast_cycle_calls_baseline_main(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest.baseline, "main", lambda: calls.append(1))
    ingest.run_forecast_cycle()
    assert calls == [1]


# ---- 【6-2】預測出錯（例如資料庫斷線）只記錄、不丟出錯誤，不能拖垮旁邊收資料的程式（對應 ingest.py 6-1）----
def test_run_forecast_cycle_swallows_errors(monkeypatch):
    """A bad cycle (e.g. a dropped Supabase connection) must never kill
    the MQTT ingest loop running alongside it in the same process."""

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ingest.baseline, "main", boom)
    ingest.run_forecast_cycle()  # must not raise


# ---- 【6-3】排程迴圈：跑一次 → 睡指定秒數 → 再跑一次（用假的 sleep 跳出無限迴圈；對應 ingest.py 6-2）----
def test_run_forecast_loop_cycles_then_sleeps_for_the_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest, "run_forecast_cycle", lambda: calls.append("cycle"))

    def fake_sleep(seconds):
        calls.append(("sleep", seconds))
        if len(calls) >= 4:
            raise KeyboardInterrupt  # break out of the otherwise-infinite loop

    with pytest.raises(KeyboardInterrupt):
        ingest.run_forecast_loop(interval=99, sleep=fake_sleep)

    assert calls == ["cycle", ("sleep", 99), "cycle", ("sleep", 99)]
