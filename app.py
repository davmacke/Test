import os, sqlite3, time, threading
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from etoro_feed import EtoroFeed, DEFAULT_SYMBOLS

DB = Path(os.getenv("DATABASE_PATH", "scanner.sqlite3"))
POLL = max(10, int(os.getenv("POLL_SECONDS", "15")))
LIMIT = max(20, min(100, int(os.getenv("UNIVERSE_LIMIT", "100"))))
st.set_page_config(page_title="eToro Paper Scanner", layout="wide")
st.title("eToro Market Scanner")
st.caption("Cloud-hosted · Paper-only · No broker order execution")

if not os.getenv("ETORO_API_KEY") or not os.getenv("ETORO_USER_KEY"):
    st.error("Set ETORO_API_KEY and ETORO_USER_KEY in your hosting provider's environment settings.")
    st.stop()

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS quotes(symbol TEXT, instrument_id INTEGER, ts TEXT, bid REAL, ask REAL, last REAL, change_pct REAL, PRIMARY KEY(symbol,ts))")
    c.execute("CREATE TABLE IF NOT EXISTS paper_positions(id INTEGER PRIMARY KEY, symbol TEXT, opened_at TEXT, entry REAL, units REAL, stop REAL, target REAL, status TEXT, closed_at TEXT, exit REAL, pnl REAL, reason TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, message TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS relative_strength(name TEXT, numerator TEXT, denominator TEXT, ts TEXT, ratio REAL, index_value REAL, PRIMARY KEY(name,ts))")
    c.commit()
    return c

def log(c, level, msg):
    c.execute("INSERT INTO events(ts,level,message) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), level, msg))
    c.commit()

def refresh_once():
    c = connect()
    try:
        feed = EtoroFeed()
        cache = DB.parent / "instrument_cache.json"
        import json
        if cache.exists():
            mapping = json.loads(cache.read_text(encoding="utf-8"))
            missing = [symbol for symbol in DEFAULT_SYMBOLS[:LIMIT] if symbol not in mapping]
            if missing:
                mapping.update(feed.resolve_symbols(missing))
                cache.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        else:
            mapping = feed.resolve_symbols(DEFAULT_SYMBOLS[:LIMIT])
            cache.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        quotes = feed.fetch_rates(mapping)
        stamp = datetime.now(timezone.utc).isoformat()
        for q in quotes:
            c.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?,?)",
                      (q["symbol"], q["instrument_id"], stamp, q.get("bid"), q.get("ask"), q.get("last"), q.get("change_pct")))
            # Existing open simulated positions only; no new entries in this first cloud version.
            rows = c.execute("SELECT id,entry,units,stop,target FROM paper_positions WHERE symbol=? AND status='OPEN'", (q["symbol"],)).fetchall()
            price = q.get("last")
            if price is not None:
                for t in rows:
                    reason = "simulated_stop" if price <= t["stop"] else ("simulated_target" if price >= t["target"] else None)
                    if reason:
                        pnl = (price - t["entry"]) * t["units"] - (price + t["entry"]) * t["units"] * 0.0015 / 2
                        c.execute("UPDATE paper_positions SET status='CLOSED',closed_at=?,exit=?,pnl=?,reason=? WHERE id=?",
                                  (stamp, price, pnl, reason, t["id"]))
        c.commit()
        # Relative-strength ratios use the same polling cycle's eToro prices.
        latest = {x["symbol"]: x.get("last") for x in quotes if x.get("last") is not None and x.get("last") > 0}
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
            if numerator not in latest or denominator not in latest:
                continue
            ratio = latest[numerator] / latest[denominator]
            first = c.execute("SELECT ratio FROM relative_strength WHERE name=? ORDER BY ts ASC LIMIT 1", (name,)).fetchone()
            base = float(first[0]) if first and first[0] else ratio
            index_value = 100.0 * ratio / base
            c.execute("INSERT OR REPLACE INTO relative_strength(name,numerator,denominator,ts,ratio,index_value) VALUES(?,?,?,?,?,?)",
                      (name, numerator, denominator, stamp, ratio, index_value))
        c.commit()
        log(c, "INFO", f"Fetched {len(quotes)} eToro rate snapshots; updated relative-strength ratios")
    except Exception as e:
        log(c, "ERROR", f"{type(e).__name__}: {e}")
    finally:
        c.close()

def worker():
    while True:
        refresh_once()
        time.sleep(POLL)

# Guard against duplicate background threads on Streamlit reruns.
if not hasattr(st, "_market_worker_started"):
    st._market_worker_started = True
    threading.Thread(target=worker, daemon=True, name="market-feed").start()

st_autorefresh(interval=POLL * 1000, key="dashboard-auto-refresh")
st.caption(f"Universe limit: {LIMIT} instruments · Refresh target: {POLL}s · eToro REST snapshots")
if st.button("Refresh page"):
    st.rerun()

c = connect()
try:
    q = pd.read_sql_query("""SELECT q.* FROM quotes q JOIN (SELECT symbol,MAX(ts) ts FROM quotes GROUP BY symbol) x
                             ON q.symbol=x.symbol AND q.ts=x.ts ORDER BY COALESCE(q.change_pct,0) DESC""", c)
    trades = pd.read_sql_query("SELECT * FROM paper_positions ORDER BY id DESC", c)
    events = pd.read_sql_query("SELECT ts,level,message FROM events ORDER BY id DESC LIMIT 25", c)
    rel = pd.read_sql_query("SELECT * FROM relative_strength ORDER BY ts", c)
    age = None
    if not q.empty:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(q.ts.max())).total_seconds()
    open_trades = int((trades.status == "OPEN").sum()) if not trades.empty else 0
    realised = float(trades.loc[trades.status == "CLOSED", "pnl"].fillna(0).sum()) if not trades.empty else 0
    a,b,d,e = st.columns(4)
    a.metric("Symbols with quotes", int(q.symbol.nunique()) if not q.empty else 0)
    b.metric("Open paper positions", open_trades)
    d.metric("Realised paper P&L", f"${realised:,.2f}")
    e.metric("Quote age", f"{int(age)}s" if age is not None else "No data")
    if age is None or age > max(POLL*4, 90):
        st.warning("Feed is stale or not connected. New paper entries are disabled.")
    st.subheader("Scanner")
    if q.empty:
        st.info("Waiting for the first successful market-data response.")
    else:
        q["spread_pct"] = ((q.ask-q.bid)/q["last"]*100).where(q["last"] > 0)
        st.dataframe(q[["symbol","last","bid","ask","spread_pct","change_pct","ts"]], use_container_width=True, hide_index=True)
    st.subheader("Market breadth & sector leadership")
    st.caption("Ratios are calculated from eToro rate snapshots. Rising lines mean the numerator is outperforming SPY; falling lines mean it is underperforming. The index is normalised to 100 at the first captured observation, so it becomes more informative as history accumulates.")
    if rel.empty:
        st.info("Waiting for matching quotes for RSP, SPY and sector ETFs. Make sure these instruments resolve in the eToro feed.")
    else:
        latest_rel = rel.sort_values("ts").groupby("name", as_index=False).tail(1).copy()
        changes = []
        now_utc = datetime.now(timezone.utc)
        for _, row in latest_rel.iterrows():
            history = rel[rel.name == row.name] if False else rel[rel["name"] == row["name"]].sort_values("ts")
            out = row.to_dict()
            for label, hours in (("1h change %", 1), ("24h change %", 24)):
                cutoff = now_utc.timestamp() - hours * 3600
                prior = None
                for _, old in history.iterrows():
                    try:
                        if datetime.fromisoformat(old["ts"]).timestamp() <= cutoff:
                            prior = old
                        else:
                            break
                    except Exception:
                        continue
                out[label] = ((float(row["index_value"]) / float(prior["index_value"]) - 1) * 100) if prior is not None and prior["index_value"] else None
            changes.append(out)
        rs = pd.DataFrame(changes)
        if not rs.empty:
            rs["direction"] = rs["1h change %"].apply(lambda v: "Rising" if pd.notna(v) and v > 0 else ("Falling" if pd.notna(v) and v < 0 else "Flat / building history"))
            st.dataframe(rs[["name", "numerator", "denominator", "ratio", "index_value", "1h change %", "24h change %", "direction", "ts"]], use_container_width=True, hide_index=True)
            chart = rel.copy()
            chart["ts"] = pd.to_datetime(chart["ts"], utc=True)
            chart = chart.pivot(index="ts", columns="name", values="index_value").sort_index()
            st.line_chart(chart, x_label="UTC time", y_label="Relative-strength index (first observation = 100)")
        st.caption("RSP/SPY measures equal-weight versus cap-weight S&P 500 leadership; it does not identify a specific sector by itself. XLV/SPY is the healthcare comparison. Paper-only: these ratios do not place trades.")
    st.subheader("Paper positions")
    st.dataframe(trades, use_container_width=True, hide_index=True)
    st.subheader("Recent events")
    st.dataframe(events, use_container_width=True, hide_index=True)
finally:
    c.close()
