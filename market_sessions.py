"""Demo-only regular-session confirmation for known UK/US equity listings.
Calendar coverage is explicit and expires; outside it, search metadata remains authoritative.
Broker execution can still reject a scheduled-open market (halts, restrictions or outages).
"""
from datetime import date,datetime,time,timezone
from zoneinfo import ZoneInfo
from data_quality import market_problem,quote_problem,age_seconds

VALID_FROM=date(2026,11,1)
VALID_THROUGH=date(2027,12,31)
SOURCES={
 "XLON":"https://www.londonstockexchange.com/en/resources/markets-products/business-days",
 "XNYS":"https://www.nyse.com/trade/hours-calendars",
}
VERIFIED_AT="2026-11-02"
CALENDARS={
 "XLON":{"zone":"Europe/London","open":"08:00","close":"16:30",
         "closed":set("2026-12-25 2026-12-28 2027-01-01 2027-03-26 2027-03-29 2027-05-03 2027-05-31 2027-08-30 2027-12-27 2027-12-28".split()),
         "early":{d:"12:30" for d in ("2026-12-24","2026-12-31","2027-12-24","2027-12-31")}},
 "XNYS":{"zone":"America/New_York","open":"09:30","close":"16:00",
         "closed":set("2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24".split()),
         "early":{d:"13:00" for d in ("2026-11-27","2026-12-24","2027-11-26")}},
}

def session(symbol,now):
    from etoro_feed import ASSET_UNIVERSE,UK_SYMBOLS
    us_equities=set(ASSET_UNIVERSE["Stocks"].split())|set(ASSET_UNIVERSE["ETFs"].split())
    code="XLON" if symbol in UK_SYMBOLS else ("XNYS" if symbol in us_equities else None)
    result={"calendar":code,"source":SOURCES.get(code),"verified_at":VERIFIED_AT,"is_open":False}
    if code is None: return dict(result,reason="No verified calendar for instrument")
    if now.tzinfo is None: return dict(result,reason="Session timestamp requires timezone")
    cfg=CALENDARS[code]; zone=ZoneInfo(cfg["zone"]); local=now.astimezone(zone); day=local.date()
    if not VALID_FROM<=day<=VALID_THROUGH:
        return dict(result,reason="Exchange calendar outside verified coverage")
    if day.weekday()>=5 or day.isoformat() in cfg["closed"]:
        return dict(result,reason="Exchange calendar holiday or weekend")
    opening=datetime.combine(day,time.fromisoformat(cfg["open"]),zone)
    closing=datetime.combine(day,time.fromisoformat(cfg["early"].get(day.isoformat(),cfg["close"])),zone)
    # Conservative pause covers the scheduled London midday auction plus a one-minute buffer.
    in_auction = code=="XLON" and time(12,0)<=local.time()<time(12,3)
    return dict(result,is_open=opening<=local<closing and not in_auction,
                reason="London midday auction buffer" if in_auction else "Regular equity session",
                opens_at=opening.astimezone(timezone.utc).isoformat(),closes_at=closing.astimezone(timezone.utc).isoformat())

def confirmed_market_status(symbol,meta,quote,now,mode="demo"):
    """Use calendar evidence only for the demonstrated unreliable isExchangeOpen fallback."""
    reason=market_problem(meta,now)
    if mode!="demo" or reason!="Market closed" or meta.get("open_status_source")!="isExchangeOpen":
        return reason,meta
    window=session(symbol,now)
    if not window["is_open"]:
        return reason,dict(meta,calendar_session=window) if window["calendar"] else meta
    quality=quote_problem(quote,now)
    if quality: return quality,meta
    if any(age_seconds(quote.get(k),now)>30 for k in ("quote_ts","fetched_at")):
        return "Calendar confirmation requires a quote within 30 seconds",meta
    verified=dict(meta,is_open=True,broker_reported_open=False,open_status_source="exchange_calendar:"+window["calendar"],
                  calendar_session=window)
    return None,verified
