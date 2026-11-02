"""Independent, deterministic veto for new demo entries; never submits or closes orders."""
import json
import math
from datetime import timedelta
from statistics import mean, median
from bollinger_rsi import completed_daily_candles
from data_quality import age_seconds, quote_problem

VERSION = "market_approval_v1"
SECTORS = tuple("XLK XLF XLE XLI XLV XLY XLP XLU XLB XLRE XLC".split())
PRIORITY = ("SPY", "UK100", "BTC", "QQQ") + SECTORS
CRYPTO = set("BTC ETH XRP SOL ADA DOGE LTC BCH LINK AVAX DOT UNI".split())
UK = set("RR.L SHEL.L BP.L HSBA.L BARC.L LLOY.L AZN.L GSK.L ULVR.L BATS.L DGE.L NG.L SSE.L RIO.L GLEN.L AAL.L BA.L TSCO.L VOD.L BT.L EZJ.L".split())
SECTOR_FOR = {}
for sector, names in {
    "XLK":"AAPL MSFT NVDA AVGO AMD INTC QCOM MU ORCL CRM ADBE PLTR PANW SMH SOXX",
    "XLC":"GOOGL META NFLX DIS CMCSA T VZ TMUS",
    "XLY":"AMZN TSLA HD LOW MCD SBUX UBER ABNB BKNG",
    "XLF":"JPM BAC WFC GS MS V MA AXP COIN HOOD",
    "XLE":"XOM CVX COP SLB OXY EOG PSX VLO XOP USO",
    "XLI":"CAT DE GE HON RTX LMT NOC BA UPS FDX",
    "XLP":"WMT COST TGT KO PEP PG",
    "XLV":"LLY JNJ MRK ABBV PFE UNH ISRG ABT TMO",
    "XLRE":"VNQ",
}.items():
    SECTOR_FOR.update({s:sector for s in names.split()})
for sector in SECTORS: SECTOR_FOR[sector] = sector
US_FUNDS = set("SPY QQQ DIA IWM RSP VOO VTI ARKK".split())
NON_EQUITY = set("GOLD SILVER OIL NATGAS COPPER PLATINUM EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD EURGBP GBPJPY GLD SLV TLT HYG".split())

def measure(payload, fetched_at, now):
    age = age_seconds(fetched_at, now)
    bars = completed_daily_candles(payload.get("approval_history", []), now)
    if age is None or not 0 <= age <= 7200:
        return None, "analysis fetch missing or older than two hours"
    if len(bars) < 55:
        return None, "need 55 completed daily candles"
    candle_age = age_seconds(bars[-1]["fromDate"], now)
    if candle_age is None or not 0 <= candle_age <= 5*86400:
        return None, "completed candle older than five calendar days"
    closes = [b["close"] for b in bars]
    return {
        "date": bars[-1]["fromDate"][:10], "close": closes[-1],
        "day_pct": 100*(closes[-1]/closes[-2]-1),
        "five_day_pct": 100*(closes[-1]/closes[-6]-1),
        "sma20": mean(closes[-20:]), "sma50": mean(closes[-50:]),
        "sma50_change": mean(closes[-50:])-mean(closes[-55:-5]),
        "lower_low": bars[-1]["low"] < bars[-2]["low"],
    }, None

def context(connection, now):
    metrics, unavailable = {}, {}
    for row in connection.execute("SELECT symbol,fetched_at,status,payload FROM market_analysis"):
        try:
            if row["status"] != "ok": raise ValueError("analysis error")
            value, error = measure(json.loads(row["payload"]), row["fetched_at"], now)
            if error: unavailable[row["symbol"]] = error
            else: metrics[row["symbol"]] = value
        except (ValueError, TypeError, KeyError):
            unavailable[row["symbol"]] = "invalid analysis"
    return {"version":VERSION, "evaluated_at":now.isoformat(), "metrics":metrics, "unavailable":unavailable,
            "method":"Completed daily candle returns, not headline sentiment or full-index constituent breadth."}

def breadth(ctx, members, date, minimum):
    values = [ctx["metrics"][s]["day_pct"] for s in members
              if s in ctx["metrics"] and ctx["metrics"][s]["date"] == date]
    return {"count":len(values), "required":minimum,
            "positive_pct":100*sum(x>0 for x in values)/len(values) if values else None,
            "median_day_pct":median(values) if values else None}

def approve(symbol, meta, quote, ctx, quotes, now):
    result = {"version":VERSION,"status":"WAIT","reasons":[],"evaluated_at":now.isoformat()}
    def stop(status, reason):
        result.update(status=status);result["reasons"].append(reason);return result
    metrics = ctx["metrics"]
    own = metrics.get(symbol)
    if not own: return stop("WAIT", symbol+": "+ctx["unavailable"].get(symbol,"daily analysis unavailable"))
    result["instrument"] = own
    # This intentionally also applies to Bollinger recovery entries: oversold alone is not enough.
    if own["day_pct"] <= -2 or own["five_day_pct"] <= -5:
        return stop("VETO","Instrument is still in a sharp daily/five-session sell-off")
    if own["close"] < own["sma50"] and own["sma50_change"] < 0:
        return stop("VETO","Instrument is below a declining 50-day average")
    if own["lower_low"] and own["day_pct"] < 0:
        return stop("VETO","Latest completed candle fell and made a lower low")
    if quote_problem(quote, now):
        return stop("WAIT","Fresh instrument quote required")
    if float(quote["bid"]) < own["close"]*.98:
        return stop("VETO","Current bid is more than 2% below the last completed close")

    if symbol in UK:
        benchmark, members, minimum = "UK100", UK, 15
        result["scope"] = "UK shares; tracked UK basket breadth"
    elif symbol in CRYPTO:
        benchmark, members, minimum = "BTC", CRYPTO, 8
        result["scope"] = "Crypto; tracked crypto basket breadth"
    elif symbol in SECTOR_FOR or symbol in US_FUNDS or symbol in {"SPX500","NSDQ100","DJ30"}:
        benchmark, members, minimum = "SPY", SECTORS, 9
        result["scope"] = "US equity; sector ETF participation proxy"
    elif symbol in NON_EQUITY:
        benchmark = None
        result["scope"] = "Instrument-only filter; no equity sentiment proxy applied"
    else:
        return stop("WAIT","No validated regional/asset approval mapping for this instrument")
    if benchmark:
        market = metrics.get(benchmark)
        result["benchmark"] = benchmark
        if not market or market["date"] != own["date"]:
            return stop("WAIT","Benchmark history unavailable or completed dates do not match")
        result["market"] = market
        b = breadth(ctx, members, market["date"], minimum);result["breadth"] = b
        if b["count"] < minimum:
            return stop("WAIT","Insufficient same-date breadth coverage")
        if market["day_pct"] <= -1.5 or market["five_day_pct"] <= -3:
            return stop("VETO","Benchmark sell-off: wait for a later completed candle to clear the threshold")
        if market["close"] < market["sma50"] and market["sma50_change"] < 0:
            return stop("VETO","Benchmark is below a declining 50-day average")
        if b["positive_pct"] < 30 and b["median_day_pct"] < -.5:
            return stop("VETO","Broad weakness across the tracked market basket")
        market_quote = quotes.get(benchmark) or {}
        if quote_problem(market_quote, now):
            return stop("WAIT","Fresh benchmark quote unavailable; intraday sell-off check cannot run")
        result["market_intraday_pct"] = 100*(float(market_quote["bid"])/market["close"]-1)
        if result["market_intraday_pct"] <= -1.5:
            return stop("VETO","Benchmark is falling more than 1.5% intraday from the completed close")
    sector = SECTOR_FOR.get(symbol)
    if sector:
        s = metrics.get(sector);result["sector"] = sector
        if not s or s["date"] != own["date"]:
            return stop("WAIT","Matching sector history unavailable")
        result["sector_metrics"] = s
        if s["day_pct"] <= -1.5 or (s["close"] < s["sma50"] and s["sma50_change"] < 0):
            return stop("VETO","Sector sell-off or declining sector trend")
    return stop("APPROVED","Market, instrument and applicable sector checks passed; sizing and broker checks still apply")

def audit(connection, symbol, strategy, decision):
    connection.execute("""CREATE TABLE IF NOT EXISTS demo_approval_events(
        id INTEGER PRIMARY KEY, evaluated_at TEXT, symbol TEXT, strategy TEXT, status TEXT, payload TEXT)""")
    connection.execute("INSERT INTO demo_approval_events(evaluated_at,symbol,strategy,status,payload) VALUES(?,?,?,?,?)",
                       (decision["evaluated_at"],symbol,strategy,decision["status"],json.dumps(decision,allow_nan=False)))
    connection.execute("DELETE FROM demo_approval_events WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM demo_approval_events)")
    connection.commit()
