"""eToro USD equity/cash accounting, following its published P&L guide."""
import math

def number(value,default=0.0):
    try:
        result=float(value)
        return result if math.isfinite(result) else default
    except (TypeError,ValueError,OverflowError): return default

def position_pnl(position):
    nested=position.get("unrealizedPnL")
    if isinstance(nested,dict) and "pnL" in nested: return number(nested["pnL"])
    for key in ("pnL","pnl","PnL","unrealizedPnL"):
        if key in position and not isinstance(position[key],dict): return number(position[key])
    return 0.0

def all_positions(portfolio):
    client=(portfolio or {}).get("clientPortfolio") or {}
    return list(client.get("positions") or [])+[p for m in client.get("mirrors") or [] for p in m.get("positions") or []]

def pending_orders(portfolio):
    c=(portfolio or {}).get("clientPortfolio") or {}
    return [o for o in c.get("ordersForOpen") or [] if number(o.get("mirrorID",o.get("mirrorId",0)))==0]+list(c.get("orders") or [])

def totals(portfolio):
    c=(portfolio or {}).get("clientPortfolio") or {}
    if "credit" not in c: raise ValueError("Broker cash balance unavailable")
    credit=number(c["credit"],None)
    if credit is None: raise ValueError("Broker cash balance invalid")
    pending=pending_orders(portfolio)
    reserved=sum(max(0,number(o.get("amount"))) for o in pending)
    costs=sum(number(o.get("totalExternalCosts")) for o in c.get("ordersForOpen") or []
              if number(o.get("mirrorID",o.get("mirrorId",0)))==0)
    positions=all_positions(portfolio)
    mirror_cash=sum(number(m.get("availableAmount"))-number(m.get("closedPositionsNetProfit")) for m in c.get("mirrors") or [])
    mirror_realized=sum(number(m.get("closedPositionsNetProfit")) for m in c.get("mirrors") or [])
    invested=sum(number(p.get("amount",p.get("initialAmountInDollars"))) for p in positions)+mirror_cash+reserved+costs
    pnl=sum(position_pnl(p) for p in positions)+mirror_realized
    cash=credit-reserved
    return {"credit_usd":credit,"cash_usd":max(0,cash),"broker_reserved_usd":reserved,
            "invested_usd":invested,"unrealized_pnl_usd":pnl,"equity_usd":cash+invested+pnl}

def identifiers(row):
    return {str(row[k]) for k in ("orderID","orderId","id","requestId","referenceId") if row.get(k) is not None}

def unreflected_reserve(rows,portfolio):
    known_positions={str(p.get("positionID",p.get("positionId"))) for p in all_positions(portfolio)}
    known_orders=set().union(*(identifiers(o) for o in pending_orders(portfolio))) if pending_orders(portfolio) else set()
    reserve=0.0
    for row in rows:
        r=dict(row)
        if r.get("action")!="open" or r.get("status") not in {"SUBMITTING","UNKNOWN","PENDING","OPEN","CLOSING","CANCELLING"}: continue
        if r.get("position_id") is not None and str(r["position_id"]) in known_positions: continue
        if {str(r[k]) for k in ("order_id","request_id") if r.get(k) is not None}&known_orders: continue
        reserve+=max(0,number(r.get("amount_usd")))
    return reserve
