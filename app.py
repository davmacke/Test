import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from analysis import analyze_candles, portfolio_risk
from etoro_feed import EtoroFeed, DEFAULT_SYMBOLS

DB = Path(os.getenv("DATABASE_PATH", "scanner.sqlite3"))
POLL = max(30, int(os.getenv("POLL_SECONDS", "60")))
LIMIT = max(20, min(100, int(os.getenv("UNIVERSE_LIMIT", "100"))))
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
st.caption("Multi-asset market signals · Portfolio risk checks · Read-only / paper workflow")

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
    connection.commit()
    return connection


def log(connection, level, message):
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
    wanted = DEFAULT_SYMBOLS[:LIMIT]
    missing = [
        symbol for symbol in wanted
        if symbol not in mapping or not mapping[symbol].get("asset_class")
    ]
    if missing:
        mapping.update(feed.resolve_symbols(missing))
        cache_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    return {symbol: mapping[symbol] for symbol in wanted if symbol in mapping}


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
        for row in connection.execute("SELECT symbol,fetched_at,status FROM market_analysis")
    }
    stale = []
    for symbol in mapping:
        row = cache_rows.get(symbol)
        if row is None:
            stale.append(symbol)
            continue
        try:
            age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            threshold = 300 if row["status"] == "error" else CANDLE_REFRESH_SECONDS
            if age >= threshold:
                stale.append(symbol)
        except (TypeError, ValueError):
            stale.append(symbol)

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
                   (symbol,instrument_id,ts,bid,ask,last,change_pct,asset_class)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    quote["symbol"], quote["instrument_id"], stamp, quote.get("bid"),
                    quote.get("ask"), quote.get("last"), quote.get("change_pct"),
                    quote.get("asset_class", "Unclassified"),
                ),
            )
        connection.commit()

        latest_prices = {
            quote["symbol"]: quote.get("last") for quote in quotes
            if quote.get("last") is not None
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

a, b, c, d = st.columns(4)
a.metric("Quoted instruments", int(quotes.symbol.nunique()) if not quotes.empty else 0)
b.metric("Open positions", risk["position_count"] if portfolio else "Unavailable")
c.metric("Estimated account value", f"{risk['estimated_account_value_usd']:,.2f} USD" if portfolio else "Unavailable")
d.metric("Quote age", f"{int(quote_age)}s" if quote_age is not None else "No data")
if quote_age is None or quote_age > max(POLL * 4, 180):
    st.warning("The quote feed is stale or not connected. Signals may be out of date.")
if portfolio_row and portfolio_row["status"] == "error":
    st.warning("The eToro portfolio read failed. Check API read scopes and Render logs.")
elif not portfolio:
    st.info("No portfolio snapshot yet. Portfolio risk and account-based sizing appear after the eToro read succeeds.")

signals_tab, risk_tab, breadth_tab, feed_tab = st.tabs(
    ["Market signals", "Portfolio risk", "Breadth & sectors", "Feed status"]
)

with signals_tab:
    st.subheader("Explainable market signals")
    st.caption(
        "Daily trend and momentum rules: 20/50-day averages, 20-day momentum, RSI, "
        "realized volatility, 90-day drawdown, and an ATR-based risk-budget size estimate. "
        "Signals are rule outputs, not guaranteed outcomes."
    )
    if analyses.empty:
        st.info("Historical candles are being loaded gradually. Initial coverage for 100 symbols can take around ten minutes.")
    else:
        records = []
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
                "Price": price,
                "RSI (14)": result.get("rsi14"),
                "20d momentum %": result.get("momentum20_pct"),
                "Annual volatility %": result.get("annual_volatility_pct"),
                "90d drawdown %": result.get("max_drawdown90_pct"),
                "Size estimate (USD)": result.get("suggested_size_usd"),
                "Why": "; ".join(result.get("reasons", [])),
                "Updated": row["fetched_at"],
            })
        signal_frame = pd.DataFrame(records)
        sort_order = {"BUY CANDIDATE": 0, "HOLD / WATCH": 1, "REDUCE / AVOID": 2}
        signal_frame["_sort"] = signal_frame["Signal"].map(sort_order).fillna(3)
        signal_frame = signal_frame.sort_values(["_sort", "Score"], ascending=[True, False]).drop(columns="_sort")
        st.dataframe(signal_frame, use_container_width=True, hide_index=True)
        with st.expander("Signal and sizing rules"):
            st.markdown(
                f"- A **Buy candidate** needs a trend/momentum score of at least 4.
"
                f"- A **Reduce / avoid** signal is shown at a score of -2 or lower; other results are Hold / Watch.
"
                f"- Size estimate uses {RISK_PER_TRADE_PCT:g}% of estimated account value at risk, "
                f"capped at {MAX_POSITION_PCT:g}% of the account. Missing portfolio data disables sizing.
"
                "- The stop gap is an ATR-based calculation used for sizing only; this app does not place or manage orders."
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
    st.write(f"Analysis uses up to {LIMIT} configured symbols. The universe includes crypto, commodities, currencies, indices, ETFs, and stocks; unsupported symbols are skipped.")
    st.write(f"Risk sizing defaults: {RISK_PER_TRADE_PCT:g}% account risk per candidate, {MAX_POSITION_PCT:g}% maximum estimated account allocation.")
    if not analyses.empty:
        status_counts = analyses["status"].value_counts().to_dict()
        st.write("Analysis cache status:", status_counts)
    if portfolio_row:
        st.write(f"Portfolio snapshot: {portfolio_row['status']} · {portfolio_row['fetched_at']}")
    st.markdown("**Latest quotes**")
    if quotes.empty:
        st.info("Waiting for the first successful eToro market-data response.")
    else:
        quote_view = quotes.copy()
        quote_view["spread_pct"] = ((quote_view["ask"] - quote_view["bid"]) / quote_view["last"] * 100).where(quote_view["last"] > 0)
        st.dataframe(
            quote_view[["symbol", "asset_class", "last", "bid", "ask", "spread_pct", "change_pct", "ts"]],
            use_container_width=True, hide_index=True,
        )
    st.markdown("**Recent service events**")
    st.dataframe(events, use_container_width=True, hide_index=True)
