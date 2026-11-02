import json
import os
import sqlite3
import subprocess
import sys
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from analysis import analyze_candles, portfolio_risk
from bollinger_rsi import analyze_bollinger_rsi, completed_daily_candles
from sector_rotation import analyze_rotation, SUPPORTED as ROTATION_SYMBOLS
from data_quality import quote_problem, age_seconds
from trade_approval import PRIORITY as APPROVAL_PRIORITY
from demo_trader import run_demo_cycle
from etoro_feed import EtoroFeed, DEFAULT_SYMBOLS, UK_SYMBOLS

DB = Path(os.getenv("DATABASE_PATH", "scanner.sqlite3"))
POLL = max(30, int(os.getenv("POLL_SECONDS", "60")))
LIMIT = max(20, min(121, int(os.getenv("UNIVERSE_LIMIT", "121"))))
MODE = os.getenv("ETORO_TRADING_MODE", "demo").strip().lower()
if MODE not in {"demo", "real"}:
    MODE = "demo"
CANDLE_REFRESH_SECONDS = max(900, int(os.getenv("ANALYSIS_REFRESH_SECONDS", "1800")))
PORTFOLIO_REFRESH_SECONDS = max(60, int(os.getenv("PORTFOLIO_REFRESH_SECONDS", "300")))
CANDLES_PER_CYCLE = max(1, min(10, int(os.getenv("CANDLES_PER_CYCLE", "10"))))
RISK_PER_TRADE_PCT = max(0.0, float(os.getenv("RISK_PER_TRADE_PCT", "0.5")))
MAX_POSITION_PCT = max(0.0, float(os.getenv("MAX_POSITION_PCT", "5")))

st.set_page_config(page_title="eToro Market & Risk Analyst", layout="wide")
st.title("eToro Market & Portfolio Analyst")
st.caption("Multi-asset market signals · Portfolio risk checks · Demo-only automatic trading")

if not os.getenv("ETORO_API_KEY") or not os.getenv("ETORO_USER_KEY"):
    st.error("Set ETORO_API_KEY and ETORO_USER_KEY in your hosting provider's environment settings.")
    st.stop()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quotes(
            symbol TEXT, instrument_id INTEGER, ts TEXT, bid REAL, ask REAL,
            last REAL, change_pct REAL, asset_class TEXT,
            PRIMARY KEY(symbol, ts)
        )
    """)
    quote_columns = {row["name"] for row in connection.execute("PRAGMA table_info(quotes)")}
    if "asset_class" not in quote_columns:
        connection.execute("ALTER TABLE quotes ADD COLUMN asset_class TEXT")
    if "quote_ts" not in quote_columns:
        try:
            connection.execute("ALTER TABLE quotes ADD COLUMN quote_ts TEXT")
        except sqlite3.OperationalError:
            if "quote_ts" not in {row["name"] for row in connection.execute("PRAGMA table_info(quotes)")}:
                raise
    connection.execute("""
        CREATE TABLE IF NOT EXISTS relative_strength(
            name TEXT, numerator TEXT, denominator TEXT, ts TEXT, ratio REAL,
            index_value REAL, PRIMARY KEY(name, ts)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS market_analysis(
            symbol TEXT PRIMARY KEY, fetched_at TEXT, status TEXT, payload TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_cache(
            id INTEGER PRIMARY KEY CHECK(id=1), fetched_at TEXT, mode TEXT,
            status TEXT, payload TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, ts TEXT, level TEXT, message TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS demo_bot_state(
            id INTEGER PRIMARY KEY CHECK(id=1), started_at TEXT, ends_at TEXT,
            status TEXT, start_unrealized_pnl REAL, last_unrealized_pnl REAL,
            start_realized_pnl REAL, last_realized_pnl REAL, stop_reason TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS demo_bot_orders(
            id INTEGER PRIMARY KEY, symbol TEXT, instrument_id INTEGER,
            request_id TEXT UNIQUE, order_id INTEGER, action TEXT, status TEXT,
            amount_usd REAL, entry_price REAL, stop_rate REAL, target_rate REAL,
            position_id INTEGER, units REAL, pnl REAL, close_order_id INTEGER,
            created_at TEXT, updated_at TEXT, message TEXT
        )
    """)
    for table, name, definition in (
        ("demo_bot_state", "run_key", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("demo_bot_orders", "run_key", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("demo_bot_orders", "strategy", "TEXT NOT NULL DEFAULT 'trend'"),
        ("demo_bot_orders", "signal_candle", "TEXT"),
    ):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                if name not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                    raise
    connection.execute("""
        CREATE TABLE IF NOT EXISTS demo_bot_snapshot(
            id INTEGER PRIMARY KEY CHECK(id=1), fetched_at TEXT, payload TEXT
        )
    """)
    connection.commit()
    return connection


def log(connection, level, message):
    print(json.dumps({"event": "market_worker", "level": level, "message": str(message)[:1000]}), flush=True)
    connection.execute(
        "INSERT INTO events(ts,level,message) VALUES(?,?,?)",
        (datetime.now(timezone.utc).isoformat(), level, str(message)[:1000]),
    )
    connection.commit()


def load_mapping(cache_path, feed):
    if cache_path.exists():
        try:
            mapping = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mapping = {}
    else:
        mapping = {}
    if not isinstance(mapping, dict):
        mapping = {}
    wanted = DEFAULT_SYMBOLS[:LIMIT]
    unresolved_raw = mapping.get("_unresolved_symbols", [])
    unresolved = set(unresolved_raw) if isinstance(unresolved_raw, list) else set()
    missing = []
    for symbol in wanted:
        meta = mapping.get(symbol)
        if symbol not in unresolved and (
            not isinstance(meta, dict)
            or not meta.get("asset_class")
            or "is_currently_tradable" not in meta
        ):
            missing.append(symbol)
    if missing:
        resolved = feed.resolve_symbols(missing)
        unresolved.update(symbol for symbol in missing if symbol not in resolved)
        mapping.update(resolved)
        mapping["_unresolved_symbols"] = sorted(unresolved)
        cache_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    dynamic_fields = {"is_open", "is_buy_enabled", "is_currently_tradable", "status_checked_at"}
    return {
        symbol: {key: value for key, value in mapping[symbol].items() if key not in dynamic_fields}
        for symbol in wanted if isinstance(mapping.get(symbol), dict)
    }


def refresh_portfolio(connection, feed):
    row = connection.execute(
        "SELECT fetched_at,mode FROM portfolio_cache WHERE id=1"
    ).fetchone()
    if row and row["mode"] == MODE:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            if age < PORTFOLIO_REFRESH_SECONDS:
                return
        except (TypeError, ValueError):
            pass
    try:
        payload = feed.fetch_portfolio(MODE)
        connection.execute(
            "INSERT OR REPLACE INTO portfolio_cache(id,fetched_at,mode,status,payload) VALUES(1,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), MODE, "ok", json.dumps(payload)),
        )
        log(connection, "INFO", f"Updated {MODE} portfolio snapshot")
    except Exception as error:
        connection.execute(
            "INSERT OR REPLACE INTO portfolio_cache(id,fetched_at,mode,status,payload) VALUES(1,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), MODE, "error", "{}"),
        )
        log(connection, "ERROR", f"Portfolio read failed: {type(error).__name__}: {error}")


def refresh_analysis(connection, feed, mapping, latest_prices, equity):
    now = datetime.now(timezone.utc)
    cache_rows = {
        row["symbol"]: row
        for row in connection.execute("SELECT symbol,fetched_at,status,payload FROM market_analysis")
    }
    stale = []
    for symbol in mapping:
        row = cache_rows.get(symbol)
        if row is None:
            stale.append(symbol)
            continue
        try:
            if row["status"] == "ok" and ("approval_history" not in json.loads(row["payload"]) or "bollinger_rsi" not in json.loads(row["payload"]) or
                    (symbol in (*ROTATION_SYMBOLS, "SPY") and "rotation_history" not in json.loads(row["payload"]))):
                stale.append(symbol)
                continue
            age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            threshold = 300 if row["status"] == "error" else CANDLE_REFRESH_SECONDS
            if age >= threshold:
                stale.append(symbol)
        except (TypeError, ValueError, json.JSONDecodeError):
            stale.append(symbol)

    stale.sort(key=lambda symbol: (symbol not in APPROVAL_PRIORITY, symbol not in UK_SYMBOLS, symbol in cache_rows))
    for symbol in stale[:CANDLES_PER_CYCLE]:
        instrument_id = mapping[symbol]["instrument_id"]
        try:
            candles = feed.fetch_candles(instrument_id, interval="OneDay", count=100)
            result = analyze_candles(
                candles,
                current_price=latest_prices.get(symbol),
                equity=equity,
                risk_per_trade_pct=RISK_PER_TRADE_PCT,
                max_position_pct=MAX_POSITION_PCT,
            )
            result["bollinger_rsi"] = analyze_bollinger_rsi(
                candles, current_price=latest_prices.get(symbol), equity=equity,
                risk_per_trade_pct=RISK_PER_TRADE_PCT, max_position_pct=MAX_POSITION_PCT,
            )
            result["approval_history"] = completed_daily_candles(candles, now)[-100:]
            if symbol in UK_SYMBOLS:
                completed = completed_daily_candles(candles, now)
                reference = completed[-1]["close"] if completed else None
                result["uk_reference_close"] = reference
                result["bollinger_rsi"]["uk_reference_close"] = reference
            if symbol in (*ROTATION_SYMBOLS, "SPY"):
                result["rotation_history"] = completed_daily_candles(candles, now)[-61:]
            bb = result["bollinger_rsi"]
            log(connection, "INFO", "BOLLINGER_RSI_ANALYSIS " + json.dumps({
                "symbol": symbol, "action": bb["action"], "completed_bars": bb["bars"],
                "signal_candle": bb.get("signal_candle"), "rsi14": bb.get("rsi14"),
            }))
            connection.execute(
                "INSERT OR REPLACE INTO market_analysis(symbol,fetched_at,status,payload) VALUES(?,?,?,?)",
                (symbol, now.isoformat(), "ok", json.dumps(result)),
            )
        except Exception as error:
            connection.execute(
                "INSERT OR REPLACE INTO market_analysis(symbol,fetched_at,status,payload) VALUES(?,?,?,?)",
                (symbol, now.isoformat(), "error", json.dumps({
                    "action": "DATA ERROR", "score": 0,
                    "reasons": [f"{type(error).__name__}: {error}"],
                    "suggested_size_usd": None,
                })),
            )
            log(connection, "ERROR", f"Candle analysis failed for {symbol}: {type(error).__name__}: {error}")

    histories = {}
    payloads = {}
    for row in connection.execute("SELECT symbol,fetched_at,status,payload FROM market_analysis"):
        if row["status"] != "ok":
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        payloads[row["symbol"]] = payload
        if "rotation_history" in payload:
            histories[row["symbol"]] = {"bars": payload["rotation_history"], "evaluated_at": row["fetched_at"]}
    rotation = analyze_rotation(histories, equity, now, RISK_PER_TRADE_PCT, MAX_POSITION_PCT)
    for symbol, signal in rotation.items():
        if symbol not in payloads:
            continue
        payloads[symbol]["sector_rotation"] = signal
        connection.execute("UPDATE market_analysis SET payload=? WHERE symbol=?",
                           (json.dumps(payloads[symbol]), symbol))
    log(connection, "INFO", "SECTOR_ROTATION_ANALYSIS " + json.dumps({
        symbol: {"action": r["action"], "rank": r.get("rank"), "coverage": r.get("sector_coverage"),
                 "relative20_pct": r.get("relative20_pct"), "reasons": r["reasons"]}
        for symbol,r in rotation.items()
    }))
    connection.commit()


def refresh_once():
    connection = connect()
    try:
        feed = EtoroFeed()
        cache_path = DB.parent / "instrument_cache.json"
        mapping = load_mapping(cache_path, feed)
        quotes = feed.fetch_rates(mapping)
        stamp = datetime.now(timezone.utc).isoformat()
        for quote in quotes:
            connection.execute(
                """INSERT OR REPLACE INTO quotes
                   (symbol,instrument_id,ts,bid,ask,last,change_pct,asset_class,quote_ts)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    quote["symbol"], quote["instrument_id"], quote.get("fetched_at") or stamp, quote.get("bid"),
                    quote.get("ask"), quote.get("last"), quote.get("change_pct"),
                    quote.get("asset_class", "Unclassified"), quote.get("quote_ts"),
                ),
            )
        connection.commit()

        latest_prices = {
            quote["symbol"]: quote.get("last") for quote in quotes
            if quote.get("last") is not None and quote_problem(quote) is None
        }
        pairs = [
            ("RSP / SPY · equal-weight breadth", "RSP", "SPY"),
            ("Healthcare / SPY", "XLV", "SPY"),
            ("Technology / SPY", "XLK", "SPY"),
            ("Financials / SPY", "XLF", "SPY"),
            ("Energy / SPY", "XLE", "SPY"),
            ("Industrials / SPY", "XLI", "SPY"),
            ("Consumer discretionary / SPY", "XLY", "SPY"),
            ("Semiconductors / SPY", "SMH", "SPY"),
        ]
        for name, numerator, denominator in pairs:
            if latest_prices.get(numerator) and latest_prices.get(denominator):
                ratio = latest_prices[numerator] / latest_prices[denominator]
                first = connection.execute(
                    "SELECT ratio FROM relative_strength WHERE name=? ORDER BY ts ASC LIMIT 1",
                    (name,),
                ).fetchone()
                base = float(first["ratio"]) if first and first["ratio"] else ratio
                connection.execute(
                    """INSERT OR REPLACE INTO relative_strength
                       (name,numerator,denominator,ts,ratio,index_value) VALUES(?,?,?,?,?,?)""",
                    (name, numerator, denominator, stamp, ratio, 100.0 * ratio / base),
                )

        refresh_portfolio(connection, feed)
        portfolio_row = connection.execute(
            "SELECT status,payload FROM portfolio_cache WHERE id=1"
        ).fetchone()
        portfolio = {}
        if portfolio_row and portfolio_row["status"] == "ok":
            try:
                portfolio = json.loads(portfolio_row["payload"])
            except json.JSONDecodeError:
                portfolio = {}
        classes = {
            str(meta["instrument_id"]): meta.get("asset_class", "Unclassified")
            for meta in mapping.values()
        }
        risk = portfolio_risk(portfolio, classes)
        refresh_analysis(
            connection, feed, mapping, latest_prices,
            risk["estimated_account_value_usd"],
        )
        uk_rows = []
        quotes_by_symbol = {q["symbol"]: q for q in quotes}
        for symbol in UK_SYMBOLS:
            row = connection.execute("SELECT status,payload FROM market_analysis WHERE symbol=?", (symbol,)).fetchone()
            payload = json.loads(row["payload"]) if row else {}
            q = quotes_by_symbol.get(symbol) or {}
            uk_rows.append({"symbol": symbol, "instrument_id": mapping.get(symbol, {}).get("instrument_id"),
                            "quote": q.get("last"), "quote_status": quote_problem(q),
                            "bars": payload.get("bars"), "reference_close": payload.get("uk_reference_close"),
                            "trend": payload.get("action"), "bollinger_rsi": (payload.get("bollinger_rsi") or {}).get("action")})
        print("UK_MARKET_STATUS " + json.dumps(uk_rows), flush=True)
        def bot_log(level, message):
            log(connection, level, message)
        demo_status = run_demo_cycle(connection, feed, mapping, quotes, portfolio, bot_log)
        connection.execute(
            "INSERT OR REPLACE INTO demo_bot_snapshot(id,fetched_at,payload) VALUES(1,?,?)",
            (datetime.now(timezone.utc).isoformat(), json.dumps(demo_status)),
        )
        connection.commit()
        quality_counts = {}
        for quote in quotes:
            reason = quote_problem(quote) or "Fresh"
            quality_counts[reason] = quality_counts.get(reason, 0) + 1
        log(connection, "INFO", "Quote quality: " + json.dumps(quality_counts))
        log(connection, "INFO", f"Fetched {len(quotes)} rate snapshots and updated market/risk analysis")
    except Exception as error:
        log(connection, "ERROR", f"{type(error).__name__}: {error}")
    finally:
        connection.close()


def worker():
    while True:
        refresh_once()
        time.sleep(POLL)


# A single process-local worker avoids starting a second poller on Streamlit reruns.
if not hasattr(st, "_market_worker_started"):
    st._market_worker_started = True
    threading.Thread(target=worker, daemon=True, name="market-feed").start()

# A separate low-priority, read-only process keeps historical replay out of the trading loop.
backtest_id = os.getenv("BACKTEST_RUN_ID", "").strip()
backtest_folder = None
if backtest_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", backtest_id):
    backtest_folder = DB.parent / "backtests" / backtest_id
    previous_job = getattr(st, "_backtest_process", None)
    if previous_job is None or getattr(st, "_backtest_id", None) != backtest_id:
        st._backtest_id = backtest_id
        st._backtest_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("backtest_runner.py")), str(DB.resolve()), backtest_id],
            cwd=str(Path(__file__).resolve().parent),
        )

research_id = os.getenv("RESEARCH_RUN_ID", "").strip()
research_folder = None
if research_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", research_id):
    research_folder = DB.parent / "research" / research_id
    if getattr(st, "_research_id", None) != research_id:
        st._research_id = research_id
        st._research_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("research_runner.py")), str(DB.resolve()), research_id],
            cwd=str(Path(__file__).resolve().parent),
        )

copy_id = os.getenv("COPY_RUN_ID", "").strip()
copy_folder = None
if copy_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", copy_id):
    copy_folder = DB.parent / "copy_research" / copy_id
    if getattr(st, "_copy_id", None) != copy_id:
        st._copy_id = copy_id
        st._copy_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("copy_runner.py")), str(DB.resolve()), copy_id],
            cwd=str(Path(__file__).resolve().parent),
        )

growth_folder = DB.parent / "growth"
if os.getenv("GROWTH_EVALUATOR_ENABLED", "true").strip().lower() in {"1","true","yes"}:
    growth_process = getattr(st, "_growth_process", None)
    if growth_process is None or growth_process.poll() is not None:
        st._growth_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("growth_runner.py")), str(DB.resolve())],
            cwd=str(Path(__file__).resolve().parent),
        )

st_autorefresh(interval=POLL * 1000, key="dashboard-auto-refresh")
st.caption(
    f"Universe: up to {LIMIT} instruments · Refresh: {POLL}s · "
    f"Portfolio: {MODE.upper()} read-only · Analysis: daily candles"
)

connection = connect()
try:
    quotes = pd.read_sql_query(
        """SELECT q.* FROM quotes q JOIN
           (SELECT symbol,MAX(ts) ts FROM quotes GROUP BY symbol) x
           ON q.symbol=x.symbol AND q.ts=x.ts
           ORDER BY COALESCE(q.change_pct,0) DESC""",
        connection,
    )
    analyses = pd.read_sql_query(
        "SELECT symbol,fetched_at,status,payload FROM market_analysis",
        connection,
    )
    relative = pd.read_sql_query(
        "SELECT * FROM relative_strength ORDER BY ts",
        connection,
    )
    events = pd.read_sql_query(
        "SELECT ts,level,message FROM events ORDER BY id DESC LIMIT 25",
        connection,
    )
    portfolio_row = connection.execute(
        "SELECT fetched_at,mode,status,payload FROM portfolio_cache WHERE id=1"
    ).fetchone()
    demo_row = connection.execute(
        "SELECT fetched_at,payload FROM demo_bot_snapshot WHERE id=1"
    ).fetchone()
    demo_orders = pd.read_sql_query(
        "SELECT symbol,run_key,strategy,signal_candle,action,status,amount_usd,entry_price,stop_rate,target_rate,pnl,message,created_at "
        "FROM demo_bot_orders ORDER BY id DESC",
        connection,
    )
finally:
    connection.close()

mapping_path = DB.parent / "instrument_cache.json"
try:
    instrument_mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.exists() else {}
except (OSError, json.JSONDecodeError):
    instrument_mapping = {}
instrument_classes = {
    str(meta.get("instrument_id")): meta.get("asset_class", "Unclassified")
    for meta in instrument_mapping.values()
    if isinstance(meta, dict)
}
portfolio = {}
if portfolio_row and portfolio_row["status"] == "ok" and portfolio_row["mode"] == MODE:
    try:
        portfolio = json.loads(portfolio_row["payload"])
    except json.JSONDecodeError:
        portfolio = {}
risk = portfolio_risk(portfolio, instrument_classes)

quote_age = None
if not quotes.empty:
    try:
        quote_age = (datetime.now(timezone.utc) - datetime.fromisoformat(quotes.ts.max())).total_seconds()
    except ValueError:
        pass

if not quotes.empty:
    quotes["quote_status"] = quotes.apply(
        lambda row: quote_problem({**row.to_dict(), "fetched_at": row["ts"]}) or "Fresh",
        axis=1,
    )

a, b, c, d = st.columns(4)
a.metric("Quoted instruments", int(quotes.symbol.nunique()) if not quotes.empty else 0)
b.metric("Open positions", risk["position_count"] if portfolio else "Unavailable")
c.metric("Estimated account value", f"{risk['estimated_account_value_usd']:,.2f} USD" if portfolio else "Unavailable")
fresh_count = int((quotes["quote_status"] == "Fresh").sum()) if not quotes.empty else 0
d.metric("Fresh broker quotes", f"{fresh_count}/{len(quotes)}")
st.caption(
    f"Last successful quote fetch: {int(quote_age)}s ago" if quote_age is not None
    else "No successful quote fetch recorded"
)
if not quotes.empty and fresh_count < len(quotes):
    st.info("Some instruments have stale or missing broker timestamps. Closed markets can show old prices. These quotes cannot trigger new bot entries.")
if quote_age is None or quote_age > max(POLL * 4, 180):
    st.warning("No recent successful quote fetch. Check the feed and bot cycle timestamps.")
if portfolio_row and portfolio_row["status"] == "error":
    st.warning("The eToro portfolio read failed. Check API read scopes and Render logs.")
elif not portfolio:
    st.info("No portfolio snapshot yet. Portfolio risk and account-based sizing appear after the eToro read succeeds.")

signals_tab, risk_tab, breadth_tab, feed_tab, bot_tab, backtest_tab, research_tab, copy_tab, growth_tab = st.tabs(
    ["Market signals", "Portfolio risk", "Breadth & sectors", "Feed status", "Demo bot", "Backtest", "Strategy research", "Disclosure followers", "Portfolio growth"]
)

with signals_tab:
    st.subheader("Explainable market signals")
    st.caption(
        "Daily trend and momentum rules: 20/50-day averages, 20-day momentum, RSI, "
        "realized volatility, 90-day drawdown, and an ATR-based risk-budget size estimate. "
        "Signals are rule outputs, not guaranteed outcomes."
    )
    if analyses.empty:
        st.info("Historical candles are being loaded gradually. Initial coverage for the configured symbols can take around ten minutes.")
    else:
        records = []
        bb_records = []
        rotation_records = []
        for _, row in analyses.iterrows():
            try:
                result = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                result = {}
            quote = quotes[quotes["symbol"] == row["symbol"]] if not quotes.empty else pd.DataFrame()
            price = quote.iloc[0]["last"] if not quote.empty else result.get("price")
            asset_class = quote.iloc[0]["asset_class"] if not quote.empty else "Unclassified"
            records.append({
                "Symbol": row["symbol"],
                "Asset class": asset_class,
                "Signal": result.get("action", row["status"].upper()),
                "Score": result.get("score"),
                "Price (broker units)": price,
                "Listing": "UK / London" if row["symbol"] in UK_SYMBOLS else "Other",
                "Quote status": quote.iloc[0]["quote_status"] if not quote.empty else "Unavailable",
                "Broker quote time": quote.iloc[0]["quote_ts"] if not quote.empty else None,
                "RSI (14)": result.get("rsi14"),
                "20d momentum %": result.get("momentum20_pct"),
                "Annual volatility %": result.get("annual_volatility_pct"),
                "90d drawdown %": result.get("max_drawdown90_pct"),
                "Size estimate (USD)": result.get("suggested_size_usd"),
                "Why": "; ".join(result.get("reasons", [])),
                "Updated": row["fetched_at"],
            })
            rotation = result.get("sector_rotation")
            if rotation:
                rotation_records.append({
                    "Symbol": row["symbol"], "Signal": rotation.get("action"),
                    "Rank": rotation.get("rank"), "Sector coverage": rotation.get("sector_coverage"),
                    "20-session relative return vs SPY %": rotation.get("relative20_pct"),
                    "20-session momentum %": rotation.get("momentum20_pct"),
                    "Stop": rotation.get("stop_rate"), "Target": rotation.get("target_rate"),
                    "Signal candle": rotation.get("signal_candle"),
                    "Why": "; ".join(rotation.get("reasons", [])),
                })
            bb = result.get("bollinger_rsi") or {}
            bb_records.append({
                "Symbol": row["symbol"], "Signal": bb.get("action", "Loading"),
                "Quote status": quote.iloc[0]["quote_status"] if not quote.empty else "Unavailable",
                "Price": price, "Wilder RSI (14)": bb.get("rsi14"),
                "Lower band / stop": bb.get("lower_band"),
                "Middle band / target": bb.get("middle_band"),
                "Upper band": bb.get("upper_band"),
                "Signal candle (UTC)": bb.get("signal_candle"),
                "Size estimate (USD)": bb.get("suggested_size_usd"),
                "Why": "; ".join(bb.get("reasons", [])),
            })
        signal_frame = pd.DataFrame(records)
        sort_order = {"BUY CANDIDATE": 0, "HOLD / WATCH": 1, "REDUCE / AVOID": 2}
        signal_frame["_sort"] = signal_frame["Signal"].map(sort_order).fillna(3)
        signal_frame = signal_frame.sort_values(["_sort", "Score"], ascending=[True, False]).drop(columns="_sort")
        st.dataframe(signal_frame, use_container_width=True, hide_index=True)
        st.subheader("Bollinger Bands + RSI · daily wick recovery")
        st.caption(
            "Completed daily candles only: 20-period bands at 2 standard deviations, Wilder RSI(14) below 30, "
            "low below the lower band and close back inside. Long entries use the signal candle's fixed "
            "lower band as stop and middle band as target. Fresh quotes and market status are required. "
            "All strategies share the bot budget; a Bollinger/RSI candidate takes priority for the same symbol."
        )
        bb_frame = pd.DataFrame(bb_records)
        if not bb_frame.empty:
            bb_frame["_sort"] = bb_frame["Signal"].map({"BUY CANDIDATE": 0, "HOLD / WATCH": 1}).fillna(2)
            st.dataframe(bb_frame.sort_values("_sort").drop(columns="_sort"),
                         use_container_width=True, hide_index=True)
        st.subheader("Oil and sector rotation")
        st.caption("Completed daily candles: sectors rank by 20-session relative return versus SPY. "
                   "Top-three leadership, positive momentum and a close above SMA50 are required, "
                   "with at least eight aligned sectors. Oil requires a 20-session breakout above SMA50. "
                   "Stops use 2× average true range (14), targets use twice the initial risk. "
                   "Bollinger entries take priority; rotation owns other entries for these instruments. "
                   "News research informs review; headlines do not automatically place orders.")
        if rotation_records:
            st.dataframe(pd.DataFrame(rotation_records), use_container_width=True, hide_index=True)
        with st.expander("Signal and sizing rules"):
            st.markdown(
                f"""
- A **Buy candidate** needs a trend/momentum score of at least 4.
- A **Reduce / avoid** signal is shown at a score of -2 or lower; other results are Hold / Watch.
- Size estimate uses {RISK_PER_TRADE_PCT:g}% of estimated account value at risk, capped at {MAX_POSITION_PCT:g}% of the account. Missing portfolio data disables sizing.
- The stop gap is ATR-based. When the demo bot is enabled, it can place demo orders with broker-side stop-loss and take-profit levels.
"""
            )

with risk_tab:
    st.subheader("Portfolio risk checks")
    st.caption(
        "Uses the eToro portfolio snapshot. Capital and gross exposure are estimates from position amount, "
        "leverage, and available credit; they may differ from the broker's live equity and liquidation values."
    )
    if not portfolio:
        st.info("Portfolio data is not available yet. Confirm read access for the selected account mode.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Invested capital", f"{risk['invested_capital_usd']:,.2f} USD")
        m2.metric("Available cash", f"{risk['cash_usd']:,.2f} USD")
        m3.metric("Estimated gross exposure", f"{risk['estimated_gross_exposure_usd']:,.2f} USD")
        m4.metric("Exposure / account value", f"{risk['leverage_multiple']:.2f}x")
        if risk["flags"]:
            for flag in risk["flags"]:
                st.warning(flag)
        else:
            st.success("No configured concentration, leverage, cash, or stop-loss flags were detected.")
        class_rows = [
            {"Asset class": name, "Estimated capital weight %": round(weight, 2)}
            for name, weight in risk["asset_class_weights"].items()
        ]
        if class_rows:
            st.markdown("**Allocation by asset class**")
            st.dataframe(pd.DataFrame(class_rows).sort_values("Estimated capital weight %", ascending=False),
                         use_container_width=True, hide_index=True)
        if risk["positions"]:
            st.markdown("**Open positions included in the estimate**")
            st.dataframe(pd.DataFrame(risk["positions"]), use_container_width=True, hide_index=True)

with breadth_tab:
    st.subheader("Market breadth & sector leadership")
    st.caption(
        "Ratios use same-cycle eToro snapshots. A rising line means the numerator is outperforming SPY; "
        "a falling line means it is underperforming. History starts at deployment."
    )
    if relative.empty:
        st.info("Waiting for matching quotes for RSP, SPY and sector ETFs.")
    else:
        latest = relative.sort_values("ts").groupby("name", as_index=False).tail(1).copy()
        changes = []
        now = datetime.now(timezone.utc)
        for _, row in latest.iterrows():
            history = relative[relative["name"] == row["name"]].sort_values("ts")
            output = row.to_dict()
            for label, hours in (("1h change %", 1), ("24h change %", 24)):
                cutoff = now.timestamp() - hours * 3600
                prior = None
                for _, old in history.iterrows():
                    try:
                        if datetime.fromisoformat(old["ts"]).timestamp() <= cutoff:
                            prior = old
                        else:
                            break
                    except (TypeError, ValueError):
                        continue
                output[label] = (
                    (float(row["index_value"]) / float(prior["index_value"]) - 1) * 100
                    if prior is not None and prior["index_value"] else None
                )
            changes.append(output)
        relative_frame = pd.DataFrame(changes)
        relative_frame["Direction"] = relative_frame["1h change %"].apply(
            lambda value: "Rising" if pd.notna(value) and value > 0
            else ("Falling" if pd.notna(value) and value < 0 else "Building history")
        )
        st.dataframe(
            relative_frame[["name", "numerator", "denominator", "ratio", "index_value",
                            "1h change %", "24h change %", "Direction", "ts"]],
            use_container_width=True, hide_index=True,
        )
        chart = relative.copy()
        chart["ts"] = pd.to_datetime(chart["ts"], utc=True)
        chart = chart.pivot(index="ts", columns="name", values="index_value").sort_index()
        st.line_chart(chart, x_label="UTC time", y_label="Relative-strength index (first observation = 100)")

with feed_tab:
    st.subheader("Market data and service status")
    st.write(f"Selected account mode: **{MODE.upper()}** (portfolio read only)")
    st.write(f"Quote polling: every {POLL} seconds; candle analysis refresh: every {CANDLE_REFRESH_SECONDS // 60} minutes.")
    st.write(f"Analysis uses up to {LIMIT} configured symbols. The universe includes crypto, commodities, currencies, indices, ETFs, US stocks and 21 London-listed UK shares; unsupported symbols are skipped.")
    st.write(f"Risk sizing defaults: {RISK_PER_TRADE_PCT:g}% account risk per candidate, {MAX_POSITION_PCT:g}% maximum estimated account allocation.")
    if not analyses.empty:
        status_counts = analyses["status"].value_counts().to_dict()
        st.write("Analysis cache status:", status_counts)
    if portfolio_row:
        st.write(f"Portfolio snapshot: {portfolio_row['status']} · {portfolio_row['fetched_at']}")
    st.caption("UK share prices and stop/target rates stay in broker quote units, commonly GBX (pence). Order budgets are USD amounts; eToro calculates the share units.")
    st.markdown("**Latest quotes**")
    if quotes.empty:
        st.info("Waiting for the first successful eToro market-data response.")
    else:
        quote_view = quotes.copy()
        quote_view["spread_pct"] = ((quote_view["ask"] - quote_view["bid"]) / quote_view["last"] * 100).where(quote_view["last"] > 0)
        st.dataframe(
            quote_view[["symbol", "asset_class", "last", "bid", "ask", "spread_pct", "change_pct", "quote_status", "quote_ts", "ts"]].rename(columns={"quote_ts": "Broker quote time", "ts": "Fetched at"}),
            use_container_width=True, hide_index=True,
        )
    st.markdown("**Recent service events**")
    st.dataframe(events, use_container_width=True, hide_index=True)

with bot_tab:
    st.subheader("Demo strategy trial")
    st.caption(
        "Execution is hard-locked to eToro's demo order endpoint. The bot is long-only at 1x leverage, "
        "sizes entries from the portfolio risk budget and caps total bot exposure "
        "at available demo cash. Each entry carries a broker-side stop loss and take profit; only bot-opened "
        "positions are closed by signal or at run end."
    )
    if demo_row:
        try:
            bot_status = json.loads(demo_row["payload"])
        except (TypeError, json.JSONDecodeError):
            bot_status = {"status": "UNKNOWN"}
    else:
        bot_status = {"status": "WAITING", "message": "Waiting for a readable demo portfolio and a running service."}
    st.caption(
        f"Run entry cap: {os.getenv('DEMO_MAX_ORDERS_PER_RUN', '5')} · "
        f"Open position cap: {os.getenv('DEMO_MAX_OPEN_POSITIONS', '5')} · "
        f"Aggregate open-loss cutoff (USD): {os.getenv('DEMO_MAX_OPEN_LOSS_USD', '25')}. "
        "Zero disables the corresponding run-level cap; strategy sizing and broker stops still apply."
    )
    st.write(f"Status: **{bot_status.get('status', 'UNKNOWN')}**")
    st.write(bot_status.get("message", ""))
    if bot_status.get("run_key"):
        st.caption(f"Run ID: {bot_status['run_key']}")
    if demo_row:
        st.caption(f"Last bot cycle: {demo_row['fetched_at']}")
        cycle_age = age_seconds(demo_row["fetched_at"])
        if cycle_age is None or cycle_age > max(POLL * 4, 180):
            st.warning("Bot status is stale. Check recent service events before assuming it is still evaluating entries.")
    st.markdown("**Independent trade approval**")
    st.caption("Every new strategy entry needs APPROVED from a separate rules-based reviewer. WAIT and VETO prevent buys; exits and broker stops remain active. These experimental thresholds have not yet demonstrated better returns.")
    st.caption("Daily filters: instrument fall ≥2% in one session or ≥5% in five, a lower low on a down day, or price below a declining 50-day average. Regional benchmark falls ≥1.5% daily/intraday or ≥3% over five sessions block entries. Weak breadth and applicable sector deterioration also veto. Missing/stale data means WAIT.")
    approval = bot_status.get("market_approval") or {}
    st.caption(f"Approval version: {approval.get('version', 'waiting')} · evaluated {approval.get('evaluated_at', 'not yet')}")
    heat = approval.get("heatmap", [])
    if heat:
        st.dataframe(pd.DataFrame(heat).style.map(lambda value: "background-color: #6b2424; color: white" if isinstance(value, (int,float)) and value < 0 else ("background-color: #205736; color: white" if isinstance(value, (int,float)) and value > 0 else ""), subset=["Daily %", "Five sessions %"]), use_container_width=True, hide_index=True)
    st.caption("Heat map uses completed eToro daily candles and shows each date. Sector ETFs are a participation proxy, not the full S&P 500 constituent heat map. UK and crypto use their own tracked baskets. Instrument-only checks apply to supported commodities/FX; unmapped assets wait. Headlines do not approve orders.")
    if bot_status.get("entry_decisions"):
        st.markdown("**Entry decisions from the last cycle**")
        st.dataframe(pd.DataFrame(bot_status["entry_decisions"]), use_container_width=True, hide_index=True)
    if bot_status.get("ends_at"):
        st.write(f"Run window: {bot_status.get('started_at')} to {bot_status.get('ends_at')} (UTC)")
    if bot_status.get("cash_budget_usd") is not None:
        st.write(
            f"Bot cash budget: {bot_status['cash_budget_usd']:,.2f} USD · "
            f"currently committed: {bot_status.get('bot_reserved_cash_usd', 0):,.2f} USD"
        )
    if bot_status.get("stop_reason"):
        st.info(bot_status["stop_reason"])
    pnl_a, pnl_b, pnl_c = st.columns(3)
    pnl_a.metric("Bot open / last-seen P&L", f"{bot_status.get('bot_open_pnl', 0):,.2f} USD")
    pnl_b.metric("Demo account unrealized P&L", f"{bot_status.get('account_unrealized_pnl_usd', 0):,.2f} USD")
    pnl_c.metric("Demo account realized P&L change", f"{bot_status.get('account_realized_change', 0):,.2f} USD")
    st.caption("Account unrealized P&L includes all current demo positions. Realized P&L change includes all activity since the run began. The Growth evaluator records a new verified equity baseline.")
    if not demo_orders.empty:
        st.markdown("**Bot order log**")
        st.dataframe(demo_orders, use_container_width=True, hide_index=True)
    st.warning(
        "This run is for observing the rules, not predicting returns. Demo fills and overnight results do not guarantee live performance."
    )


with backtest_tab:
    st.subheader("Historical strategy replay")
    st.caption("A read-only historical process runs alongside demo trading. Results are simulated, "
               "not eToro fills or a promise of income. Base and stress scenarios use assumed costs.")
    if backtest_folder is None:
        st.info("No historical backtest has been configured.")
    else:
        status_file = backtest_folder / "status.json"
        report_file = backtest_folder / "report.json"
        if status_file.exists():
            try:
                job_status = json.loads(status_file.read_text(encoding="utf-8"))
                st.write(f"Status: **{job_status.get('phase', 'UNKNOWN')}**")
                st.caption(f"Job {backtest_id} · updated {job_status.get('updated_at')}")
                if "done" in job_status:
                    st.write(f"Progress: {job_status['done']} / {job_status.get('total', '?')}")
                if job_status.get("error_type"):
                    st.error(f"Backtest failed: {job_status['error_type']}. See Render backtest logs.")
            except (OSError, ValueError):
                st.info("Backtest status is being updated.")
        else:
            st.info("Waiting for the historical worker to initialize.")
        if report_file.exists():
            try:
                report_text = report_file.read_text(encoding="utf-8")
                report = json.loads(report_text)
                st.caption(f"Historical snapshot: {report['as_of']} · initial simulated capital: "
                           f"{report['initial_capital_usd']:,.2f} USD per strategy/window")
                if report.get("sterling"):
                    sterling=report["sterling"]
                    st.caption(f"Sterling scenario: £{sterling['initial_gbp']:,.2f} initial capital · "
                               f"£{sterling['target_gbp']:g} target · GBPUSD {sterling['gbpusd']:.5f} "
                               f"at {sterling['rate_date']}. Constant FX conversion; no currency-cost reconstruction.")
                summaries = pd.DataFrame(report["summaries"])
                for window, label in [("latest_25pct", "Latest 25% · separate evaluation"),
                                      ("earlier_75pct", "Earlier 75% · historical comparison")]:
                    st.markdown(f"**{label}**")
                    frame = summaries[summaries["window"] == window].drop(
                        columns=["realized_weekly_pnl_usd"], errors="ignore")
                    st.dataframe(frame, use_container_width=True, hide_index=True)
                st.markdown("**SPY buy-and-hold comparator · fully invested**")
                st.dataframe(pd.DataFrame(report["benchmarks"]), use_container_width=True, hide_index=True)
                curve_rows = []
                for key, rows in report["curves"].items():
                    if key.startswith("latest_25pct|") and key.endswith("|base"):
                        for row in rows:
                            curve_rows.append({"Date": row["date"], "Strategy": key.split("|")[1],
                                               "Equity USD": row["equity"]})
                if curve_rows:
                    chart = pd.DataFrame(curve_rows).pivot(index="Date", columns="Strategy", values="Equity USD")
                    st.line_chart(chart)
                with st.expander("Coverage and modeling assumptions"):
                    for note in report["assumptions"]:
                        st.write("• " + note)
                    st.write("Symbols skipped:", report.get("data_failures", {}))
                    st.dataframe(pd.DataFrame([{"Symbol": s, **v} for s,v in report["coverage"].items()]),
                                 use_container_width=True, hide_index=True)
                st.download_button("Download full backtest report", report_text,
                                   file_name=f"backtest-{backtest_id}.json", mime="application/json")
                trades = [{"Window / strategy / costs": key, **trade}
                          for key, rows in report["trades"].items() for trade in rows]
                st.download_button("Download simulated trades", pd.DataFrame(trades).to_csv(index=False),
                                   file_name=f"backtest-trades-{backtest_id}.csv", mime="text/csv")
            except (OSError, ValueError, KeyError):
                st.warning("The backtest report could not be read. Check the backtest worker status.")


with research_tab:
    st.subheader("Three-strategy research · £1,250")
    st.caption("Independent simulated accounts for filtered mean reversion, cointegrated pairs and a "
               "walk-forward neural network. These strategies do not submit broker orders. "
               "The 10% monthly return and £25 withdrawal targets are measured, not promised.")
    if research_folder is None:
        st.info("No three-strategy research run has been configured.")
    else:
        status_path = research_folder / "status.json"
        report_path = research_folder / "report.json"
        if status_path.exists():
            try:
                research_status = json.loads(status_path.read_text(encoding="utf-8"))
                st.write(f"Status: **{research_status.get('phase', 'UNKNOWN')}**")
                st.caption(f"Job {research_id} · updated {research_status.get('updated_at')}")
                if "done" in research_status:
                    st.write(f"Progress: {research_status['done']} / {research_status.get('total', '?')}")
                if research_status.get("error_type"):
                    st.error(f"Research failed: {research_status['error_type']}. Check Render research logs.")
            except (OSError, ValueError):
                st.info("Research status is being updated.")
        else:
            st.info("Waiting for the research worker.")
        if report_path.exists():
            try:
                research_text = report_path.read_text(encoding="utf-8")
                study = json.loads(research_text)
                st.caption(f"Frozen history: {study['as_of']} · GBPUSD {study['gbpusd']:.5f} "
                           f"at {study['fx_date']} · initial £{study['initial_gbp']:,.2f} per comparison")
                st.warning("Cost profiles are product scenarios, not verified account-specific quotes. "
                           "Pairs use CFD costs for both legs even in the underlying-ETF comparison.")
                overview = pd.DataFrame(study["summaries"])
                for window,label in [("latest_25pct","Latest dates · separate evaluation"),
                                     ("earlier_75pct","Earlier historical comparison")]:
                    st.markdown(f"**{label}**")
                    st.dataframe(overview[overview["window"] == window], use_container_width=True, hide_index=True)
                monthly_rows = [{"Comparison": key, **month}
                                for key, rows in study["monthly"].items() for month in rows]
                st.markdown("**Monthly returns and simulated profit withdrawals**")
                st.dataframe(pd.DataFrame(monthly_rows), use_container_width=True, hide_index=True)
                st.markdown("**SPY buy-and-hold · fully invested, no withdrawals**")
                st.dataframe(pd.DataFrame(study["benchmarks"]), use_container_width=True, hide_index=True)
                curve_rows = [{"Date": row["date"], "Strategy": key.split("|")[1],
                               "Economic wealth GBP": row["economic_wealth_gbp"]}
                              for key, rows in study["curves"].items()
                              if key.startswith("latest_25pct|") and key.endswith("|underlying_base")
                              for row in rows]
                if curve_rows:
                    chart = pd.DataFrame(curve_rows).pivot(index="Date",columns="Strategy",values="Economic wealth GBP")
                    st.line_chart(chart)
                with st.expander("Rules, assumptions and model diagnostics"):
                    for name,rule in study["rules"].items():
                        st.write(f"{name}: {rule}")
                    for note in study["assumptions"]:
                        st.write("• " + note)
                    st.write("Neural prediction diagnostics:", study["neural_diagnostics"])
                    st.dataframe(pd.DataFrame(study["folds"]), use_container_width=True, hide_index=True)
                    st.dataframe(pd.DataFrame(study["pair_tests"]), use_container_width=True, hide_index=True)
                    st.dataframe(pd.DataFrame([{"Symbol": s, **v} for s,v in study["coverage"].items()]),
                                 use_container_width=True, hide_index=True)
                st.download_button("Download three-strategy research report", research_text,
                                   file_name=f"research-{research_id}.json",mime="application/json")
                st.download_button("Download research monthly results", pd.DataFrame(monthly_rows).to_csv(index=False),
                                   file_name=f"research-monthly-{research_id}.csv",mime="text/csv")
            except (OSError,ValueError,KeyError):
                st.warning("The research report could not be read. Check the worker status.")

with copy_tab:
    st.subheader("Pelosi household & Berkshire disclosure research")
    st.caption("Research only · £1,250 each · public disclosures through August 2026 · no broker orders")
    st.write("The Pelosi follower buys ordinary stock names after disclosure; options, exercises and gifts are excluded. "
             "The Berkshire follower tracks its ten largest public 13F positions after filing, preserving cash for omitted holdings.")
    if copy_folder is None:
        st.info("No disclosure study configured.")
    else:
        status_file = copy_folder / "status.json"
        report_file = copy_folder / "report.json"
        if status_file.exists():
            try:
                copy_status = json.loads(status_file.read_text(encoding="utf-8"))
                st.write(f"Status: **{copy_status.get('phase', 'UNKNOWN')}**")
                if copy_status.get("error_type"):
                    st.error(f"Research failed: {copy_status['error_type']}. Check COPY logs.")
                if copy_status.get("failures"):
                    st.warning(f"Unavailable price histories: {copy_status['failures']}")
            except (OSError, ValueError):
                st.info("Research status updating.")
        if report_file.exists():
            try:
                copy_text = report_file.read_text(encoding="utf-8")
                copy_study = json.loads(copy_text)
                st.caption(f"Frozen prices: {copy_study['as_of']} · GBPUSD {copy_study['gbpusd']:.5f} · excludes dividends and historical FX")
                st.warning("This is delayed, partial portfolio copying. Historical returns do not establish a dependable 10% monthly income.")
                copy_profile = st.selectbox("Product cost scenario", list(dict.fromkeys(r["profile"] for r in copy_study["summaries"])), key="copy-profile")
                copy_window = st.selectbox("Evaluation window", list(dict.fromkeys(r["window"] for r in copy_study["summaries"])), index=1, key="copy-window")
                copy_lag = st.selectbox("Extra disclosure delay (sessions)", [0, 5], key="copy-lag")
                shown = [r for r in copy_study["summaries"] if r["window"] == copy_window and r["profile"] == copy_profile and r["extra_delay_sessions"] == copy_lag]
                st.dataframe(pd.DataFrame(shown), use_container_width=True, hide_index=True)
                selected_runs = [r for r in copy_study["runs"] if r["window"] == copy_window and r["profile"] == copy_profile and r["extra_delay_sessions"] == copy_lag]
                curve_rows = [dict(date=p["date"], strategy=r["strategy"], value=p["wealth_gbp"]) for r in selected_runs for p in r["curve"]]
                if curve_rows:
                    curve_frame = pd.DataFrame(curve_rows).pivot(index="date",columns="strategy",values="value")
                    st.line_chart(curve_frame)
                monthly = [dict(strategy=r["strategy"], **m) for r in selected_runs for m in r["months"]]
                st.dataframe(pd.DataFrame(monthly), use_container_width=True, hide_index=True)
                with st.expander("Rules, coverage and source filings"):
                    for strategy, rule in copy_study["rules"].items():
                        st.write(f"**{strategy}**: {rule}")
                    for assumption in copy_study["assumptions"]:
                        st.write(assumption)
                    st.dataframe(pd.DataFrame(copy_study["coverage"]).T, use_container_width=True)
                    st.json(copy_study["filing_sources"])
                st.download_button("Download disclosure research report", copy_text, file_name=f"copy-{copy_id}.json",mime="application/json")
                st.download_button("Download selected monthly results", pd.DataFrame(monthly).to_csv(index=False),file_name=f"copy-monthly-{copy_id}.csv",mime="text/csv")
            except (OSError, ValueError, KeyError):
                st.warning("Disclosure report could not be read. Check worker status.")

with growth_tab:
    st.subheader("Portfolio growth and benchmark evaluator")
    st.caption("Separate read-only hourly process · entire eToro demo account · persistent observations")
    st.write("This records a new equity baseline, compares USD and GBP growth with stock-price and interest-rate benchmarks, "
             "and evaluates the current holdings. Manual and bot positions both contribute to whole-account results.")
    growth_report = growth_folder / "report.json"
    growth_status = growth_folder / "status.json"
    if growth_status.exists():
        try:
            gs = json.loads(growth_status.read_text(encoding="utf-8"))
            st.write(f"Evaluator: **{gs.get('phase', 'UNKNOWN')}** · updated {gs.get('updated_at')}")
            if gs.get("error_type"):
                st.warning(f"Evaluator retrying after {gs['error_type']}. Trading runs in its separate worker.")
        except (OSError, ValueError):
            st.info("Evaluator status updating.")
    if not growth_report.exists():
        st.info("Waiting for the evaluator's first fresh portfolio snapshot and historical coverage checks.")
    else:
        try:
            growth_text = growth_report.read_text(encoding="utf-8")
            g = json.loads(growth_text)
            g1,g2,g3 = st.columns(3)
            g1.metric("Account equity", f"{g['equity_usd']:,.2f} USD")
            g2.metric("Available cash", f"{g['cash_usd']:,.2f} USD")
            g3.metric("Account unrealized P&L", f"{g['unrealized_pnl_usd']:,.2f} USD")
            st.caption(f"Baseline {g['baseline_at']} · {g['observations']} observations · {g['elapsed_days']:.2f} elapsed days")
            st.write(g["return_method"])
            if g.get("fx_reference"):
                st.caption(f"GBP conversion: {g['fx_reference']['source']} · reference date {g['fx_reference']['as_of']}")
                if g["fx_reference"].get("approximate"):
                    st.info(g["fx_reference"]["note"])
            currency = st.selectbox("Growth comparison currency", ["GBP","USD"],key="growth-currency")
            curve = pd.DataFrame(g["curve"]).set_index("stamp")
            columns = [f"Portfolio {currency}", f"{currency} cash proxy", f"SPY_{currency}", f"QQQ_{currency}", f"UK100_{currency}"]
            st.line_chart(curve[[c for c in columns if c in curve]])
            st.caption("Normalized to100 at baseline. Stock benchmarks are price-only and exclude dividend reinvestment. Cash proxies are hypothetical, not deposit products or interest earned on eToro.")
            st.write("Tracking statistics:", {k:g.get(k) for k in ("return_usd_pct","return_gbp_pct","max_observed_drawdown_pct","complete_daily_returns","annual_volatility_pct","sharpe","cagr_pct")})
            st.info(g["metrics_status"])
            st.subheader("Dated interest-rate benchmarks")
            rates = [dict(currency=c, **r) for c,r in g["latest_rates"].items()]
            st.dataframe(pd.DataFrame(rates),use_container_width=True,hide_index=True)
            if g.get("rate_fetch_errors"):
                st.caption(f"Live source fetch limitations: {g['rate_fetch_errors']}. Seed/cached rates retain their source dates and expiry.")
            st.subheader("Current holdings and stock performance")
            st.dataframe(pd.DataFrame(g["holdings"]),use_container_width=True,hide_index=True)
            risk = g["risk"]
            st.write(f"Historical risk status: **{risk['status']}** · invested-value coverage {risk['holding_value_coverage_pct']:.1f}%")
            st.caption(risk["note"])
            if risk.get("status") == "CURRENT_HOLDINGS_PROXY":
                st.write("Proxy annual volatility %:", risk.get("annual_volatility_pct"))
                st.write("GARCH conditional-risk diagnostic:", risk.get("garch"))
                st.dataframe(pd.DataFrame(risk.get("variance_contributions",[])),use_container_width=True,hide_index=True)
                st.dataframe(pd.DataFrame(risk.get("correlations",[])),use_container_width=True,hide_index=True)
                st.caption(risk["calendar"])
            st.subheader("Execution quality")
            st.dataframe(pd.DataFrame(g.get("execution_quality",[])),use_container_width=True,hide_index=True)
            st.caption("Reconstructed nearest preceding mid quote to fill; includes spread, excludes commissions, and is not a market-impact estimate.")
            with st.expander("Research methods and limitations"):
                st.dataframe(pd.DataFrame(g["papers"]),use_container_width=True,hide_index=True)
                for note in g["limitations"]:
                    st.write(note)
                if g.get("history_failures"):
                    st.write("Historical data failures:", g["history_failures"])
                st.write("External cash-flow adjustments are read from the evaluator's flows.json ledger. A sale of an existing holding is an internal trade.")
            st.download_button("Download growth evaluation",growth_text,file_name="portfolio-growth.json",mime="application/json")
            st.download_button("Download growth observations",pd.DataFrame(g["curve"]).to_csv(index=False),file_name="portfolio-growth.csv",mime="text/csv")
        except (OSError,ValueError,KeyError):
            st.warning("Growth report is updating or could not be read. Check evaluator status.")
