#!/usr/bin/env python3
"""
worker/stock_alerts.py — piggybacks the user's separate `auto-stock-picker`
app's "5日均線跌破警示" job onto THIS already-paid, always-on Render
worker, instead of relying solely on that other repo's GitHub Actions
schedule — same problem (GitHub's `schedule` trigger is best-effort and
unreliable) and same fix (an already-running, already-paid-for process is
a much more reliable clock) as this worker's own AI forecast job; see
ingest.py's module docstring for that precedent.

This is a PORT, not a shared import: auto-stock-picker
(github.com/Bonny-sun/auto-stock-picker) is a separate repo/app (a stock
watchlist + screener), not something this repo depends on at the code
level. Its own scripts/check_price_alerts.py is the logic this was ported
from — if that script changes, this file needs the same change made by
hand; there's no automatic sync between the two repos.

Talks to the SAME Supabase project this worker's aqua_* tables live in —
it's literally named "auto-stock-picker" (see docs/architecture.md: it's
the user's pre-existing stock-picker project, reused here because
Supabase's free tier caps a user at 2 projects) — but a DIFFERENT set of
tables (`watchlist`, `alert_push_log`) that THIS repo doesn't own and
never creates or migrates. This module assumes those tables already exist
(they do — the stock-picker app's own init_db() already created them in
production); if they're ever missing, this fails loudly with a plain
Postgres "relation does not exist" error the next time it runs, which is
the correct behavior. This worker taking over schema-migration
responsibility for a different app's tables would be a much worse failure
mode than that.

Connects via psycopg2 + STOCK_DATABASE_URL, which MUST be Supabase's
connection-POOLER string (IPv4) — e.g. postgresql://...@aws-0-<region>.
pooler.supabase.com:6543/postgres — copied from Supabase's dashboard
("Connect" -> Connection pooling), NOT the direct db.<ref>.supabase.co
host (IPv6-only) that auto-stock-picker's own GitHub Actions workflow
uses (GitHub-hosted runners have outbound IPv6; Render does not — see
ingest.py's own module docstring for why THIS worker already uses REST,
not psycopg2, for its own aqua_* tables; the stock side has to dodge the
exact same problem the opposite way, by switching the connection string
instead of the client library, since porting the existing watchlist CRUD
to PostgREST would also mean sorting out a service-role-equivalent key for
a table that was never set up with Supabase Auth in mind).

Pushes through a SEPARATE LINE Official Account
(STOCK_LINE_CHANNEL_ACCESS_TOKEN) — this must never be set to the
aquaponics system's own LINE_CHANNEL_TOKEN, or stock alerts would go to
aquaponics friends and vice versa.

Fires twice on weekdays (~09:13 and ~12:13 Asia/Taipei), matching
auto-stock-picker's own GitHub Actions cron times exactly (see
.github/workflows/price_alert.yml there: "13 1 * * 1-5" / "13 4 * * 1-5"
in UTC). That GitHub Actions workflow is deliberately left enabled as a
backup, not replaced — see run_stock_alert_cycle()'s own docstring for why
a backup firing late or alongside this one is harmless, not a double push.

Env vars (Render dashboard -> Environment), both optional as a pair — if
either is unset, this feature is silently disabled and nothing else in
this worker changes (same "missing secret = feature off" pattern as
LINE_CHANNEL_TOKEN elsewhere in this worker):
  STOCK_DATABASE_URL               Supabase pooler connection string (see
                                    above) for the shared "auto-stock-
                                    picker" Supabase project.
  STOCK_LINE_CHANNEL_ACCESS_TOKEN  auto-stock-picker's own LINE Messaging
                                    API channel access token.
"""
from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

# Same local-testing-only .env loader as ingest.py's own 【1-1】 — a no-op
# in production (Render injects real env vars, no .env file there), but
# needed here too so `python stock_alerts.py` works standalone for a
# manual test (see the __main__ block at the bottom of this file), not
# only when imported by ingest.py (which already loads .env before this
# module is imported — calling it again here is harmless).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

STOCK_DATABASE_URL = os.environ.get("STOCK_DATABASE_URL", "")
STOCK_LINE_TOKEN = os.environ.get("STOCK_LINE_CHANNEL_ACCESS_TOKEN", "")

LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
_MAX_MESSAGE_LEN = 4900  # LINE's own 5000-char cap on a text message, with headroom
_TAIPEI_TZ = timezone(timedelta(hours=8))

_TWSE_SNAPSHOT_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
_TPEX_SNAPSHOT_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
_SNAPSHOT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; esp32_1-stock-alerts/1.0)"}
_name_map_cache = None

# Weekday Taipei times this fires at — mirrors auto-stock-picker's own
# GitHub Actions cron exactly (see this module's own docstring). Checked
# once a minute against wall-clock time by run_stock_alert_loop() below,
# not a fixed sleep interval like ingest.py's forecast loop, so it
# actually lands on these times instead of drifting.
_STOCK_ALERT_TIMES = {(9, 13), (12, 13)}


def _pg_connection():
    import psycopg2  # imported lazily so a missing STOCK_DATABASE_URL never requires the dep

    return psycopg2.connect(STOCK_DATABASE_URL)


def _taipei_slot() -> tuple[str, str]:
    """Same split as auto-stock-picker's own check_price_alerts.py:
    (today's date, 'morning' or 'noon'), boundary at Taipei 11:00."""
    now_tw = datetime.now(timezone.utc).astimezone(_TAIPEI_TZ)
    slot_name = "morning" if now_tw.hour < 11 else "noon"
    return now_tw.strftime("%Y-%m-%d"), slot_name


def _ticker_code(ticker: str) -> str:
    return ticker.replace(".TWO", "").replace(".TW", "")


def _claim_alert_slot(slot_date: str, slot_name: str, source: str) -> bool:
    """Same row-claim pattern as auto-stock-picker's own
    modules.database.claim_alert_slot() — a plain INSERT ... ON CONFLICT
    DO NOTHING against the SAME alert_push_log table that repo's app
    already created in this shared DB, so this worker and that repo's
    GitHub Actions workflow can never both do the real work (the DB
    round trip, the yfinance scan, the LINE push) for the same slot — one
    of them wins the INSERT, the other gets 0 rows affected and returns
    immediately without fetching anything."""
    conn = _pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alert_push_log (slot_date, slot_name, source, claimed_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (slot_date, slot_name) DO NOTHING
            """,
            (slot_date, slot_name, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        claimed = cur.rowcount == 1
        conn.commit()
        return claimed
    finally:
        conn.close()


def _fetch_watchlist_with_buy_price() -> list[dict]:
    conn = _pg_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT username, ticker, strategy_type, buy_price
            FROM watchlist
            WHERE buy_price IS NOT NULL AND notify_enabled = 1
        """)
        rows = cur.fetchall()
    finally:
        conn.close()
    result = []
    for username, ticker, strategy_type, buy_price in rows:
        buy_price = float(buy_price)
        if math.isnan(buy_price):
            # dirty data (NaN isn't NULL, so the WHERE above doesn't filter it) —
            # same skip-and-log auto-stock-picker's own script does, rather
            # than letting a NaN leak into a pnl% calculation later
            print(f"[stock-alert] skip {username}/{ticker}/{strategy_type}: buy_price is NaN", flush=True)
            continue
        result.append({"username": username, "ticker": ticker, "strategy_type": strategy_type, "buy_price": buy_price})
    return result


def _fetch_stock_name_map() -> dict:
    """TWSE/TPEx official open-data snapshots, for tickers a user is
    tracking that aren't in any fixed name table this module doesn't
    carry — unlike auto-stock-picker's own resolve_name(), there's no
    local STOCK_NAMES dict checked first here: this job runs twice a day
    on a handful of tickers, so keeping a ~150-entry dict in sync across
    two repos isn't worth it just to skip two cheap HTTP calls."""
    name_map = {}
    try:
        resp = requests.get(_TWSE_SNAPSHOT_URL, headers=_SNAPSHOT_HEADERS, timeout=15)
        resp.raise_for_status()
        for item in resp.json():
            code = str(item.get("Code") or "").strip()
            name = str(item.get("Name") or "").strip()
            if code and name:
                name_map[f"{code}.TW"] = name
    except Exception as exc:
        print(f"[stock-alert] TWSE name snapshot failed: {exc}", flush=True)
    try:
        resp = requests.get(_TPEX_SNAPSHOT_URL, headers=_SNAPSHOT_HEADERS, timeout=15)
        resp.raise_for_status()
        for item in resp.json():
            code = str(item.get("SecuritiesCompanyCode") or "").strip()
            name = str(item.get("CompanyName") or "").strip()
            if code and name:
                name_map[f"{code}.TWO"] = name
    except Exception as exc:
        print(f"[stock-alert] TPEx name snapshot failed: {exc}", flush=True)
    return name_map


def _resolve_name(ticker: str) -> str:
    global _name_map_cache
    if _name_map_cache is None:
        _name_map_cache = _fetch_stock_name_map()
    return _name_map_cache.get(ticker, _ticker_code(ticker))


def _fetch_close_and_ma5(ticker: str):
    """Returns (latest close, 5-day moving average), or None if there's
    not enough trading history.

    Found live (2026-11-19, a manual test run just after midnight Taipei
    time — see docs/architecture.md's decision log): yfinance's `end`
    date reaches one day past "today" on purpose (see below), and when
    "today" hasn't actually traded yet (pre-market, or — as here — the
    calendar day has only just begun), the row it returns for that date
    has NaN OHLC values instead of simply being absent. Taking `.iloc[-1]`
    on the raw frame (as auto-stock-picker's own fetch_close_and_ma5()
    does, which this was ported from) then picks up that NaN row as "the
    latest close" — and `nan < ma5`/`nan >= ma5` are BOTH False in Python,
    so a genuine breach silently reads as "no alert" instead of erroring
    out loudly. Fixed by dropping NaN rows before taking the latest close
    and the 5-day average, so both always come from the last REAL trade,
    whatever row index that ends up being."""
    end = datetime.today() + timedelta(days=1)
    start = end - timedelta(days=30)  # 30 calendar days, well over 5 trading days even with holidays
    df = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), auto_adjust=True)
    if df.empty:
        return None
    closes = df["Close"].dropna()
    if len(closes) < 5:
        return None
    close = float(closes.iloc[-1])
    ma5 = float(closes.tail(5).mean())
    return close, ma5


def _build_ok_message(checked_count: int) -> str:
    return (
        f"✅ 5日均線檢查完成（{datetime.now().strftime('%Y-%m-%d')}）\n"
        f"已檢查 {checked_count} 檔設定買入價格的股票，目前皆在5日均線之上，無警示。"
    )


def _build_no_watchlist_message() -> str:
    return (
        f"✅ 5日均線檢查完成（{datetime.now().strftime('%Y-%m-%d')}）\n"
        f"目前沒有股票設定買入價格，無需檢查。"
    )


def _build_message(alerts: list[dict]) -> str:
    sorted_alerts = sorted(alerts, key=lambda a: a["pnl_pct"] if a["pnl_pct"] is not None else float("inf"))
    blocks = [f"📉 跌破5日均線警示（{datetime.now().strftime('%Y-%m-%d')}）"]
    for i, a in enumerate(sorted_alerts, 1):
        pnl_str = f"{a['pnl_pct']:+.1f}%" if a["pnl_pct"] is not None else "無資料"
        blocks.append(
            f"({i}) {_ticker_code(a['ticker'])} {a['name']}｜{a['strategy_type']}頁｜{a['username']}\n"
            f"現價 {a['close']:.1f} ＜ 5日均線 {a['ma5']:.1f}\n"
            f"買入 {a['buy_price']:.1f}  損益 {pnl_str}"
        )
    text = "\n\n".join(blocks)
    if len(text) > _MAX_MESSAGE_LEN:
        text = text[:_MAX_MESSAGE_LEN] + "\n...(訊息過長，已截斷)"
    return text


def _send_line_message(text: str) -> None:
    resp = requests.post(
        LINE_BROADCAST_URL,
        headers={"Authorization": f"Bearer {STOCK_LINE_TOKEN}", "Content-Type": "application/json"},
        json={"messages": [{"type": "text", "text": text}]},
        timeout=15,
    )
    resp.raise_for_status()


def run_stock_alert_cycle(*, dedup: bool = True) -> None:
    """One pass: claim this Taipei morning/noon slot, scan the watchlist,
    push one LINE message. A backup source (auto-stock-picker's own
    GitHub Actions workflow) firing for the same slot — before, after, or
    never firing at all — is harmless either way: whichever source's
    INSERT in _claim_alert_slot() lands first does the real work (the
    yfinance scan below), and the other gets 0 rows affected and returns
    immediately, with no duplicate fetch and no duplicate push.

    Never raises — the caller (run_stock_alert_loop(), or whatever runs
    this inside ingest.py's own worker loop) must never let a stock-side
    failure take down this process's actual job, MQTT ingest.

    `dedup=False` skips the slot-claim entirely — same as auto-stock-
    picker's own check_price_alerts.py only claiming a slot when
    PRICE_ALERT_DEDUP=1 (set only for its real GitHub Actions `schedule`
    trigger, never for a manual `workflow_dispatch` run). Used by this
    module's own `python stock_alerts.py` CLI entry point below, so a
    manual test run neither consumes today's slot (which would make the
    REAL scheduled run at 09:13/12:13 skip itself, thinking it already
    fired) nor gets blocked by a slot a real run already claimed."""
    if not STOCK_DATABASE_URL or not STOCK_LINE_TOKEN:
        return

    if dedup:
        slot_date, slot_name = _taipei_slot()
        if not _claim_alert_slot(slot_date, slot_name, "esp32_1_worker"):
            print(f"[stock-alert] {slot_date} {slot_name} already claimed by another source, skipping", flush=True)
            return
        print(f"[stock-alert] claimed {slot_date} {slot_name}", flush=True)
    else:
        print("[stock-alert] manual run (dedup bypassed, not claiming a slot)", flush=True)

    watchlist_rows = _fetch_watchlist_with_buy_price()
    if not watchlist_rows:
        _send_line_message(_build_no_watchlist_message())
        print("[stock-alert] pushed (no tickers with a buy price set)", flush=True)
        return

    by_ticker = defaultdict(list)
    for row in watchlist_rows:
        by_ticker[row["ticker"]].append(row)
    print(f"[stock-alert] {len(by_ticker)} ticker(s) with a buy price set, checking 5-day MA...", flush=True)

    alerts = []
    checked_count = 0
    for ticker, rows in by_ticker.items():
        try:
            result = _fetch_close_and_ma5(ticker)
        except Exception as exc:
            print(f"[stock-alert] {ticker} fetch failed: {exc}", flush=True)
            continue
        if result is None:
            print(f"[stock-alert] {ticker} not enough trading data", flush=True)
            continue
        checked_count += 1
        close, ma5 = result
        if close < ma5:
            name = _resolve_name(ticker)
            for row in rows:
                buy_price = row["buy_price"]
                pnl_pct = (close - buy_price) / buy_price * 100 if buy_price else None
                alerts.append({
                    "ticker": ticker, "name": name, "close": close, "ma5": ma5,
                    "buy_price": buy_price, "pnl_pct": pnl_pct,
                    "username": row["username"], "strategy_type": row["strategy_type"],
                })
            print(f"[stock-alert] {ticker} close {close:.2f} < MA5 {ma5:.2f}", flush=True)
        else:
            print(f"[stock-alert] {ticker} close {close:.2f} >= MA5 {ma5:.2f}", flush=True)

    if alerts:
        _send_line_message(_build_message(alerts))
        print(f"[stock-alert] pushed {len(alerts)} alert(s)", flush=True)
    else:
        _send_line_message(_build_ok_message(checked_count))
        print("[stock-alert] pushed (nothing broke the 5-day MA)", flush=True)


def run_stock_alert_loop(
    *,
    sleep=time.sleep,
    now=lambda: datetime.now(timezone.utc),
    cycle=run_stock_alert_cycle,
) -> None:
    """Runs forever in its own thread (see ingest.py's main()): once a
    minute, checks whether "now" (Taipei time, weekday) matches one of
    _STOCK_ALERT_TIMES, and if so runs one cycle. `fired` remembers which
    (date, hour, minute) this process already triggered on, so a 60s poll
    landing on the exact same minute twice in a row can't fire twice —
    belt-and-braces alongside _claim_alert_slot(), which is the real
    cross-process/cross-repo guard against a duplicate push.

    `now` and `cycle` are injectable (defaulting to the real wall clock
    and the real run_stock_alert_cycle) purely so tests can drive this
    deterministically without actually waiting on real time."""
    fired = set()
    while True:
        now_tw = now().astimezone(_TAIPEI_TZ)
        key = (now_tw.date(), now_tw.hour, now_tw.minute)
        if now_tw.weekday() < 5 and (now_tw.hour, now_tw.minute) in _STOCK_ALERT_TIMES and key not in fired:
            fired.add(key)
            try:
                cycle()
            except Exception as exc:              # noqa: BLE001 - must never kill the worker
                print(f"[stock-alert] cycle failed: {exc}", flush=True)
        sleep(60)


# Lets this be run standalone for a manual test (e.g. locally, with
# STOCK_DATABASE_URL/STOCK_LINE_CHANNEL_ACCESS_TOKEN set in the shell),
# without waiting for 09:13/12:13 Taipei — same manual-run purpose as
# auto-stock-picker's own "python scripts/check_price_alerts.py". Not used
# by ingest.py, which calls run_stock_alert_loop()/run_stock_alert_cycle()
# directly as a library, not as a subprocess.
if __name__ == "__main__":
    run_stock_alert_cycle(dedup=False)
