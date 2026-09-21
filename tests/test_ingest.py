import json

import ingest


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


# ---- _fmt_taipei -------------------------------------------------------
def test_fmt_taipei_converts_utc_to_plus_8():
    assert ingest._fmt_taipei("2026-09-16T03:12:00Z") == "2026-09-16 11:12"


def test_fmt_taipei_crosses_midnight():
    assert ingest._fmt_taipei("2026-09-16T20:30:00+00:00") == "2026-09-17 04:30"


def test_fmt_taipei_falls_back_to_raw_string():
    assert ingest._fmt_taipei("not a date") == "not a date"
    assert ingest._fmt_taipei(None) == "None"


# ---- handle_payload ----------------------------------------------------
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


def test_handle_payload_omits_ts_when_device_clock_unsynced(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", lambda _d: None)

    ingest.handle_payload("aquaponics/home/esp32-aqua-01/telemetry", _payload(ts=None))

    assert "ts" not in client.queries[1].op("insert")[0][0]  # DB default now() applies


def test_handle_payload_device_id_falls_back_to_topic(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", lambda _d: None)

    ingest.handle_payload("aquaponics/home/board-7/telemetry", _payload(device_id=None))

    assert client.queries[1].op("insert")[0][0]["device_id"] == "board-7"


def test_handle_payload_ignores_bad_json(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)

    ingest.handle_payload("aquaponics/home/x/telemetry", b"{not json")

    assert client.queries == []


def test_handle_payload_db_error_does_not_raise_or_notify(monkeypatch):
    class Boom:
        def table(self, _name):
            raise RuntimeError("db down")

    called = []
    monkeypatch.setattr(ingest, "sb", Boom())
    monkeypatch.setattr(ingest, "notify_pending_threshold_alerts", called.append)

    ingest.handle_payload("aquaponics/home/x/telemetry", _payload())

    assert called == []


# ---- LINE alert formatting / claiming ---------------------------------
def _alert(value, lo=22, hi=31.7, metric="temperature"):
    return {"ts": "2026-09-16T03:12:00Z", "metric": metric, "value": value,
            "min_val": lo, "max_val": hi}


def _run_notify(monkeypatch, fake_client, rows):
    client = fake_client(lambda q: rows)
    pushed = []
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "push_line", pushed.append)
    ingest.notify_pending_threshold_alerts("esp32-aqua-01")
    return client, pushed


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


def test_alert_message_below_lower_bound(monkeypatch, fake_client):
    _, pushed = _run_notify(monkeypatch, fake_client, [_alert(35, lo=40, hi=60, metric="humidity")])
    assert "項目：濕度" in pushed[0]
    assert "數值：35 %RH" in pushed[0]
    assert "判讀：低於下限，正常 40–60" in pushed[0]


def test_alert_claim_is_a_single_atomic_update(monkeypatch, fake_client):
    client, _ = _run_notify(monkeypatch, fake_client, [_alert(31.9)])
    (q,) = client.queries  # one round trip: no select-then-update race
    assert q.table == "aqua_anomalies"
    assert q.has("update") and not q.has("select")
    assert q.op("is_")[0] == ("notified_at", "null")


def test_notify_is_noop_without_line_token(monkeypatch, fake_client):
    client = fake_client()
    monkeypatch.setattr(ingest, "sb", client)
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "")
    ingest.notify_pending_threshold_alerts("esp32-aqua-01")
    assert client.queries == []


# ---- push_line / kill switch ------------------------------------------
def test_line_push_paused_reads_setting(monkeypatch, fake_client):
    monkeypatch.setattr(ingest, "sb", fake_client(lambda q: [{"value": True}]))
    assert ingest.line_push_paused() is True


def test_line_push_paused_fails_open_on_db_error(monkeypatch):
    class Boom:
        def table(self, _name):
            raise RuntimeError("db down")

    monkeypatch.setattr(ingest, "sb", Boom())
    assert ingest.line_push_paused() is False  # never swallow a real alert


def test_push_line_skipped_when_paused(monkeypatch):
    posts = []
    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda: True)
    monkeypatch.setattr(ingest.requests, "post", lambda *a, **k: posts.append(a))
    ingest.push_line("hi")
    assert posts == []


def test_push_line_posts_broadcast_with_bearer_token(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda: False)
    monkeypatch.setattr(ingest.requests, "post", lambda url, **k: calls.append((url, k)) or Resp())

    ingest.push_line("hello")

    (url, kw), = calls
    assert url == ingest.LINE_BROADCAST_URL
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["json"] == {"messages": [{"type": "text", "text": "hello"}]}


def test_push_line_swallows_network_errors(monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError("offline")

    monkeypatch.setattr(ingest, "LINE_CHANNEL_TOKEN", "tok")
    monkeypatch.setattr(ingest, "line_push_paused", lambda: False)
    monkeypatch.setattr(ingest.requests, "post", boom)
    ingest.push_line("hello")  # must not raise — ingest keeps running
