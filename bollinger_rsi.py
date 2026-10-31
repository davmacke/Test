"""Daily Bollinger wick recovery with Wilder RSI; no broker side effects."""
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from data_quality import utc_timestamp, age_seconds

STRATEGY = "bollinger_rsi_v1"


def completed_daily_candles(candles, now=None):
    now = now or datetime.now(timezone.utc)
    rows = {}
    for candle in candles or []:
        stamp = utc_timestamp(candle.get("fromDate"))
        if stamp is None or stamp + timedelta(days=1) > now:
            continue
        try:
            close = float(candle["close"])
            high = float(candle["high"])
            low = float(candle["low"])
            opening = float(candle.get("open", close))
            if not all(math.isfinite(x) and x > 0 for x in (close, high, low, opening)):
                continue
            if not (low <= close <= high and low <= opening <= high):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        rows[stamp] = {"fromDate": stamp.isoformat(), "open": opening,
                       "high": high, "low": low, "close": close}
    return [rows[key] for key in sorted(rows)]


def wilder_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    changes = [b-a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period
    for delta in changes[period:]:
        gain = (gain * (period-1) + max(delta, 0)) / period
        loss = (loss * (period-1) + max(-delta, 0)) / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def bollinger_bands(closes):
    if len(closes) < 20:
        return None
    window = closes[-20:]
    middle = mean(window)
    deviation = 2 * pstdev(window)
    return middle - deviation, middle, middle + deviation


def analyze_bollinger_rsi(candles, current_price=None, equity=0.0,
                         risk_per_trade_pct=0.5, max_position_pct=5.0, now=None):
    now = now or datetime.now(timezone.utc)
    rows = completed_daily_candles(candles, now)
    result = {
        "strategy": STRATEGY, "action": "INSUFFICIENT DATA", "bars": len(rows),
        "evaluated_at": now.isoformat(), "reasons": [],
        "suggested_size_usd": None,
        "risk_per_trade_pct": float(risk_per_trade_pct),
        "max_position_pct": float(max_position_pct),
    }
    if len(rows) < 20:
        result["reasons"] = [f"Need 20 completed daily candles; received {len(rows)}"]
        return result
    closes = [row["close"] for row in rows]
    lower, middle, upper = bollinger_bands(closes)
    rsi = wilder_rsi(closes)
    signal = rows[-1]
    signal_closed = utc_timestamp(signal["fromDate"]) + timedelta(days=1)
    pierced = signal["low"] < lower
    recovered = lower < signal["close"] < upper
    oversold = rsi is not None and rsi < 30
    result.update({
        "action": "BUY CANDIDATE" if pierced and recovered and oversold and lower > 0 else "HOLD / WATCH",
        "lower_band": lower, "middle_band": middle, "upper_band": upper,
        "rsi14": rsi, "signal_candle": signal["fromDate"],
        "signal_closed_at": signal_closed.isoformat(),
        "signal_low": signal["low"], "signal_close": signal["close"],
        "stop_rate": lower, "target_rate": middle,
        "reasons": [
            "Wick pierced lower band" if pierced else "No lower-band wick pierce",
            "Candle closed back inside bands" if recovered else "Candle did not close back inside bands",
            f"Wilder RSI(14) {rsi:.2f}; requires below 30",
        ],
    })
    if result["action"] == "BUY CANDIDATE":
        price = current_price if current_price is not None else signal["close"]
        plan, reason = plan_bollinger_entry(result, price, equity, now)
        if plan:
            result["suggested_size_usd"] = plan["suggested_size_usd"]
            result["stop_gap_pct"] = plan["stop_gap_pct"]
        elif reason:
            result["reasons"].append(reason)
    return result


def plan_bollinger_entry(signal, price, equity, now=None):
    now = now or datetime.now(timezone.utc)
    if signal.get("strategy") != STRATEGY or signal.get("action") != "BUY CANDIDATE":
        return None, "No Bollinger/RSI entry signal"
    age = age_seconds(signal.get("evaluated_at"), now)
    closed = utc_timestamp(signal.get("signal_closed_at"))
    if age is None or age < -5 or age > 3600 or closed is None or closed > now:
        return None, "Bollinger/RSI analysis is stale or candle is unfinished"
    try:
        price, equity = float(price), float(equity)
        stop, target = float(signal["stop_rate"]), float(signal["target_rate"])
        risk_pct = float(signal["risk_per_trade_pct"])
        allocation_pct = float(signal["max_position_pct"])
        if not all(math.isfinite(x) for x in (price, equity, stop, target, risk_pct, allocation_pct)):
            raise ValueError()
        if not 0 < stop < price < target:
            return None, "Live entry price must lie above lower-band stop and below middle-band target"
        if equity <= 0 or risk_pct <= 0 or allocation_pct <= 0:
            return None, "Portfolio risk sizing unavailable"
        gap = (price - stop) / price * 100
        amount = min(equity * allocation_pct / 100, equity * risk_pct / gap)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "Invalid Bollinger/RSI entry inputs"
    return {
        "strategy": STRATEGY, "stop_rate": stop, "target_rate": target,
        "stop_gap_pct": gap, "suggested_size_usd": math.floor(amount * 100) / 100,
        "signal_candle": signal.get("signal_candle"),
    }, None
