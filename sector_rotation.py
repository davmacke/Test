"""Long-only daily sector rotation and oil breakout; no headline execution."""
import math
from datetime import datetime, timezone, timedelta
from statistics import mean
from bollinger_rsi import completed_daily_candles
from data_quality import age_seconds, utc_timestamp

STRATEGY = "oil_sector_rotation_v1"
SECTORS = ("XLE", "XLK", "XLF", "XLI", "XLV", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")
OIL = ("OIL", "USO")
SUPPORTED = SECTORS + OIL

def _usable(item, now):
    age = age_seconds(item.get("evaluated_at"), now)
    rows = completed_daily_candles(item.get("bars") or [], now)
    if age is None or not -5 <= age <= 3600 or len(rows) < 51:
        return []
    end = utc_timestamp(rows[-1]["fromDate"]) + timedelta(days=1)
    if (now-end).total_seconds() > 4*86400:
        return []
    return rows

def analyze_rotation(histories, equity=0, now=None, risk_per_trade_pct=.5, max_position_pct=5):
    now = now or datetime.now(timezone.utc)
    series = {s: _usable(v, now) for s,v in histories.items()}
    benchmark = series.get("SPY") or []
    results = {s: {"strategy": STRATEGY, "action": "INSUFFICIENT DATA",
        "evaluated_at": now.isoformat(), "reasons": ["Waiting for fresh aligned daily history"],
        "risk_per_trade_pct": risk_per_trade_pct, "max_position_pct": max_position_pct}
        for s in SUPPORTED}
    eligible = {}
    if benchmark:
        # Require exactly the same completed sessions: never compare mismatched dates.
        dates = [r["fromDate"] for r in benchmark[-51:]]
        spy_return = benchmark[-1]["close"]/benchmark[-21]["close"]-1
        for symbol in SECTORS:
            rows = series.get(symbol) or []
            if not rows or rows[-1]["fromDate"] != benchmark[-1]["fromDate"]:
                continue
            by_date = {r["fromDate"]: r for r in rows}
            if not all(d in by_date for d in dates):
                continue
            aligned = [by_date[d] for d in dates]
            momentum = aligned[-1]["close"]/aligned[-21]["close"]-1
            relative = (1+momentum)/(1+spy_return)-1
            eligible[symbol] = (relative, momentum, aligned)
        ranks = {s:i+1 for i,(s,_) in enumerate(sorted(eligible.items(), key=lambda x:(-x[1][0],x[0])))}
        for symbol,(relative,momentum,rows) in eligible.items():
            close = rows[-1]["close"]
            sma = mean(r["close"] for r in rows[-50:])
            enough = len(eligible) >= 8
            buy = enough and ranks[symbol] <= 3 and relative > 0 and momentum > 0 and close > sma
            action = "BUY CANDIDATE" if buy else ("REDUCE / AVOID" if close < sma or momentum <= 0 else "HOLD / WATCH")
            results[symbol].update(action=action if enough else "INSUFFICIENT DATA",
                rank=ranks[symbol], sector_coverage=len(eligible),
                relative20_pct=relative*100, momentum20_pct=momentum*100, sma50=sma,
                signal_candle=rows[-1]["fromDate"],
                reasons=[f"20-session rank {ranks[symbol]} of {len(eligible)} sectors; top 3 required",
                         f"Relative return vs SPY {relative*100:.2f}%; absolute {momentum*100:.2f}%",
                         "Close above SMA50" if close>sma else "Close below SMA50"])
            _levels(results[symbol], rows, equity)
    # Prefer direct OIL when history exists; USO is a fallback, not a second oil bet.
    chosen = next((s for s in OIL if series.get(s)), None)
    for symbol in OIL:
        rows = series.get(symbol) or []
        if not rows:
            continue
        close = rows[-1]["close"]
        sma = mean(r["close"] for r in rows[-50:])
        breakout = max(r["high"] for r in rows[-21:-1])
        momentum = rows[-1]["close"]/rows[-21]["close"]-1
        buy = symbol == chosen and close>breakout and close>sma and momentum>0
        action = "BUY CANDIDATE" if buy else ("REDUCE / AVOID" if close<sma else "HOLD / WATCH")
        results[symbol].update(action=action,signal_candle=rows[-1]["fromDate"],
            momentum20_pct=momentum*100,sma50=sma,breakout20=breakout,
            reasons=[f"Close must exceed prior 20-session high {breakout:.4f} and SMA50",
                     "Preferred oil instrument" if symbol==chosen else f"Oil fallback inactive; prefer {chosen}"])
        _levels(results[symbol],rows,equity)
    return results

def _levels(result, rows, equity):
    close = rows[-1]["close"]
    ranges = [max(rows[i]["high"]-rows[i]["low"],
                  abs(rows[i]["high"]-rows[i-1]["close"]),
                  abs(rows[i]["low"]-rows[i-1]["close"])) for i in range(len(rows)-14,len(rows))]
    distance = 2*mean(ranges)
    result.update(stop_rate=close-distance,target_rate=close+2*distance,
        signal_closed_at=(utc_timestamp(rows[-1]["fromDate"])+timedelta(days=1)).isoformat())
    plan, reason = plan_rotation_entry(result,close,equity,utc_timestamp(result["evaluated_at"]))
    if plan:
        result.update(plan)
    elif result["action"]=="BUY CANDIDATE":
        result["reasons"].append(reason)

def plan_rotation_entry(signal, price, equity, now=None):
    now = now or datetime.now(timezone.utc)
    age = age_seconds(signal.get("evaluated_at"),now)
    end = utc_timestamp(signal.get("signal_closed_at"))
    if signal.get("strategy")!=STRATEGY or signal.get("action")!="BUY CANDIDATE":
        return None,"No rotation entry signal"
    if age is None or not -5<=age<=3600 or end is None or end>now:
        return None,"Rotation analysis stale or candle unfinished"
    try:
        price,equity,stop,target,risk,allocation = map(float,(price,equity,signal["stop_rate"],
            signal["target_rate"],signal["risk_per_trade_pct"],signal["max_position_pct"]))
        if not all(math.isfinite(v) for v in (price,equity,stop,target,risk,allocation)):
            raise ValueError()
        if not 0<stop<price<target or min(equity,risk,allocation)<=0:
            raise ValueError()
        gap=(price-stop)/price*100
        amount=min(equity*allocation/100,equity*risk/gap)
    except (KeyError,ValueError,TypeError,OverflowError):
        return None,"Rotation price outside fixed stop/target or sizing unavailable"
    return {"strategy":STRATEGY,"stop_rate":stop,"target_rate":target,"stop_gap_pct":gap,
        "suggested_size_usd":math.floor(amount*100)/100,"signal_candle":signal.get("signal_candle")},None
