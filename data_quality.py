"""Broker timestamps and fail-closed entry data checks."""
import math
from datetime import datetime, timezone

MAX_QUOTE_AGE_SECONDS = 180
MAX_STATUS_AGE_SECONDS = 120

def utc_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None

def age_seconds(value, now=None):
    stamp = utc_timestamp(value)
    if stamp is None:
        return None
    return ((now or datetime.now(timezone.utc)) - stamp).total_seconds()

def quote_problem(quote, now=None):
    now = now or datetime.now(timezone.utc)
    for key, label in (("quote_ts", "Broker quote"), ("fetched_at", "Quote fetch")):
        age = age_seconds(quote.get(key), now)
        if age is None:
            return label + " timestamp unavailable"
        if age < -5:
            return label + " timestamp is in the future"
        if age > MAX_QUOTE_AGE_SECONDS:
            return label + " is stale"
    try:
        bid, ask = float(quote.get("bid")), float(quote.get("ask"))
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
            return "Invalid bid/ask quote"
    except (ValueError, TypeError, OverflowError):
        return "Invalid bid/ask quote"
    return None

def explicit_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"true", "1", "yes"}:
            return True
        if value in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None

def market_problem(meta, now=None):
    age = age_seconds(meta.get("status_checked_at"), now)
    if age is None or age < -5 or age > MAX_STATUS_AGE_SECONDS:
        return "Market status unavailable or stale"
    for key, blocked in (("is_currently_tradable", "Instrument not tradable"),
                         ("is_buy_enabled", "Buying disabled"),
                         ("is_open", "Market closed")):
        flag = explicit_flag(meta.get(key))
        if flag is None:
            return "Market status unknown: " + key
        if not flag:
            return blocked
    return None
