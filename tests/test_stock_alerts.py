"""
tests/test_stock_alerts.py — worker/stock_alerts.py 的測試。

這個檔案是從另一個 repo（auto-stock-picker）移植過來、在這個 worker 裡
常駐執行的排程工作（見 stock_alerts.py 自己的說明）。測試重點：
1. 沒設環境變數時整個功能是 no-op（不連資料庫、不打 LINE）
2. 排程迴圈只在平日的指定時間觸發、同一分鐘不重複觸發
3. 推播文字組出來的格式正確（損益排序、過長截斷）
4. 時段判斷（早上/中午）跟股票代碼格式化的小工具函式

不連真正的資料庫、不呼叫真正的 yfinance/LINE API，都用假函式/monkeypatch
代替。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import stock_alerts

_TAIPEI = timezone(timedelta(hours=8))


# ============================================================================
# 小工具函式
# ============================================================================

class _FrozenDatetime(datetime):
    """datetime subclass whose .now() always returns a fixed instant —
    used to monkeypatch stock_alerts.datetime for deterministic tests of
    functions that call datetime.now() internally (not injectable the way
    run_stock_alert_loop's own `now=` parameter is)."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.astimezone(tz) if tz else cls._fixed.replace(tzinfo=None)


def _frozen_at(fixed: datetime):
    frozen = type("_Frozen", (_FrozenDatetime,), {"_fixed": fixed})
    return frozen


def test_taipei_slot_before_11_is_morning(monkeypatch):
    # 2026-01-05 is a Monday; 09:00 Taipei
    fixed = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(stock_alerts, "datetime", _frozen_at(fixed))
    slot_date, slot_name = stock_alerts._taipei_slot()
    assert slot_date == "2026-01-05"
    assert slot_name == "morning"


def test_taipei_slot_after_11_is_noon(monkeypatch):
    fixed = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)  # 13:00 Taipei
    monkeypatch.setattr(stock_alerts, "datetime", _frozen_at(fixed))
    slot_date, slot_name = stock_alerts._taipei_slot()
    assert slot_date == "2026-01-05"
    assert slot_name == "noon"


def test_ticker_code_strips_tw_and_two_suffixes():
    assert stock_alerts._ticker_code("2330.TW") == "2330"
    assert stock_alerts._ticker_code("6523.TWO") == "6523"


# ============================================================================
# run_stock_alert_cycle — 沒設環境變數時整個功能是 no-op
# ============================================================================

def test_cycle_is_a_noop_without_both_env_vars(monkeypatch):
    """Missing either STOCK_DATABASE_URL or STOCK_LINE_CHANNEL_ACCESS_TOKEN
    must disable the whole feature silently — same "missing secret =
    feature off" pattern as LINE_CHANNEL_TOKEN elsewhere in this worker.
    Must never even try to open a DB connection."""
    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "tok")

    def boom(*_a, **_k):
        raise AssertionError("must not touch the DB when STOCK_DATABASE_URL is unset")

    monkeypatch.setattr(stock_alerts, "_pg_connection", boom)
    stock_alerts.run_stock_alert_cycle()  # must not raise, must not call _pg_connection

    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "")
    stock_alerts.run_stock_alert_cycle()  # same, other var missing


# ============================================================================
# run_stock_alert_cycle — claim 失敗（別的來源已經推播過）就直接跳過
# ============================================================================

def test_cycle_skips_when_slot_already_claimed(monkeypatch):
    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "tok")
    monkeypatch.setattr(stock_alerts, "_claim_alert_slot", lambda *a: False)

    def boom(*_a, **_k):
        raise AssertionError("must not scan the watchlist when the slot was lost")

    monkeypatch.setattr(stock_alerts, "_fetch_watchlist_with_buy_price", boom)
    stock_alerts.run_stock_alert_cycle()  # must not raise, must not scan


def test_cycle_with_dedup_false_never_claims_a_slot(monkeypatch):
    """The manual-test CLI path (dedup=False) must never touch
    _claim_alert_slot at all — claiming today's slot from a manual test
    run would make the REAL 09:13/12:13 scheduled run think it already
    fired and skip itself."""
    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "tok")

    def boom(*_a, **_k):
        raise AssertionError("dedup=False must not call _claim_alert_slot")

    monkeypatch.setattr(stock_alerts, "_claim_alert_slot", boom)
    monkeypatch.setattr(stock_alerts, "_fetch_watchlist_with_buy_price", lambda: [])
    sent = []
    monkeypatch.setattr(stock_alerts, "_send_line_message", lambda text: sent.append(text))
    stock_alerts.run_stock_alert_cycle(dedup=False)
    assert len(sent) == 1


def test_cycle_pushes_no_watchlist_message_when_nothing_tracked(monkeypatch):
    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "tok")
    monkeypatch.setattr(stock_alerts, "_claim_alert_slot", lambda *a: True)
    monkeypatch.setattr(stock_alerts, "_fetch_watchlist_with_buy_price", lambda: [])
    sent = []
    monkeypatch.setattr(stock_alerts, "_send_line_message", lambda text: sent.append(text))
    stock_alerts.run_stock_alert_cycle()
    assert len(sent) == 1
    assert "沒有股票設定買入價格" in sent[0]


def test_cycle_pushes_alert_when_price_breaks_below_ma5(monkeypatch):
    monkeypatch.setattr(stock_alerts, "STOCK_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(stock_alerts, "STOCK_LINE_TOKEN", "tok")
    monkeypatch.setattr(stock_alerts, "_claim_alert_slot", lambda *a: True)
    monkeypatch.setattr(
        stock_alerts,
        "_fetch_watchlist_with_buy_price",
        lambda: [{"username": "admin", "ticker": "2330.TW", "strategy_type": "A", "buy_price": 1000.0}],
    )
    monkeypatch.setattr(stock_alerts, "_fetch_close_and_ma5", lambda ticker: (900.0, 950.0))
    monkeypatch.setattr(stock_alerts, "_resolve_name", lambda ticker: "台積電")
    sent = []
    monkeypatch.setattr(stock_alerts, "_send_line_message", lambda text: sent.append(text))
    stock_alerts.run_stock_alert_cycle()
    assert len(sent) == 1
    assert "台積電" in sent[0] and "跌破5日均線警示" in sent[0]


# ============================================================================
# 推播訊息組字
# ============================================================================

def _alert(**overrides):
    base = {
        "ticker": "2330.TW", "name": "台積電", "close": 900, "ma5": 950,
        "buy_price": 1000, "pnl_pct": -10.0, "username": "a", "strategy_type": "A",
    }
    base.update(overrides)
    return base


def test_build_message_sorts_by_worst_pnl_first():
    alerts = [
        _alert(ticker="1101.TW", name="台泥", close=30, ma5=31, buy_price=32, pnl_pct=-6.25),
        _alert(ticker="2330.TW", name="台積電", close=900, ma5=950, buy_price=1000, pnl_pct=-10.0),
    ]
    text = stock_alerts._build_message(alerts)
    assert text.index("台積電") < text.index("台泥")  # worse pnl (-10%) listed first


def test_build_message_handles_missing_pnl():
    alerts = [_alert(buy_price=0, pnl_pct=None)]
    text = stock_alerts._build_message(alerts)
    assert "無資料" in text


def test_build_message_truncates_overlong_text():
    alerts = [
        _alert(ticker=f"{1000 + i}.TW", name=f"股票{i}" * 50, close=1, ma5=2, buy_price=3, pnl_pct=-1.0)
        for i in range(50)
    ]
    text = stock_alerts._build_message(alerts)
    assert len(text) <= stock_alerts._MAX_MESSAGE_LEN + len("\n...(訊息過長，已截斷)")
    assert text.endswith("已截斷)")


# ============================================================================
# run_stock_alert_loop — 只在平日指定時間觸發，同一分鐘不重複
# ============================================================================

def test_loop_fires_only_at_scheduled_weekday_times(monkeypatch):
    # Monday 2026-01-05, 09:13 Taipei = 01:13 UTC
    fixed = datetime(2026, 1, 5, 1, 13, tzinfo=timezone.utc)
    calls = []
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stock_alerts.run_stock_alert_loop(
            sleep=fake_sleep,
            now=lambda: fixed,
            cycle=lambda: calls.append("fired"),
        )
    assert calls == ["fired"]  # fired once, not once per poll


def test_loop_does_not_fire_outside_scheduled_times(monkeypatch):
    # Monday 2026-01-05, 10:00 Taipei (not 09:13 or 12:13) = 02:00 UTC
    fixed = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    calls = []
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stock_alerts.run_stock_alert_loop(
            sleep=fake_sleep,
            now=lambda: fixed,
            cycle=lambda: calls.append("fired"),
        )
    assert calls == []


def test_loop_does_not_fire_on_weekends(monkeypatch):
    # Saturday 2026-01-03, 09:13 Taipei = 01:13 UTC
    fixed = datetime(2026, 1, 3, 1, 13, tzinfo=timezone.utc)
    calls = []
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        stock_alerts.run_stock_alert_loop(
            sleep=fake_sleep,
            now=lambda: fixed,
            cycle=lambda: calls.append("fired"),
        )
    assert calls == []


def test_loop_swallows_cycle_errors(monkeypatch):
    """A broken stock-alert cycle (e.g. yfinance or the DB down) must
    never kill this worker's actual job, MQTT ingest, running alongside it
    — same contract as ingest.py's own run_forecast_cycle."""
    fixed = datetime(2026, 1, 5, 1, 13, tzinfo=timezone.utc)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 1:
            raise KeyboardInterrupt

    def boom():
        raise RuntimeError("db down")

    with pytest.raises(KeyboardInterrupt):
        stock_alerts.run_stock_alert_loop(sleep=fake_sleep, now=lambda: fixed, cycle=boom)
