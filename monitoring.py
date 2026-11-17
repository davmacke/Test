"""Read-only demo performance and headline triage. Never submits orders."""
from collections import defaultdict
import re

CRITICAL = re.compile(r"\\b(resign(?:s|ed|ation)?|arrest(?:ed)?|fraud|espionage|spy|investigation|bankrupt(?:cy)?|accounting irregularit(?:y|ies)|guidance cut|profit warning|sanction(?:s|ed)?|recall)\\b", re.I)
TICKERS = {"NVDA":r"\\b(?:nvidia|jensen huang|nvda)\\b","AAPL":r"\\b(?:apple|aapl)\\b","MSFT":r"\\b(?:microsoft|msft)\\b","SHEL.L":r"\\b(?:shell plc|shell shares)\\b","BP.L":r"\\b(?:bp plc|bp shares)\\b"}
def performance(orders, run_key=None):
    rows=[dict(o) for o in orders if (run_key is None or o.get("run_key")==run_key) and o.get("strategy")!="execution_check"]
    filled=[o for o in rows if o.get("status") in ("OPEN","CLOSING","CLOSED")]
    closed=[o for o in rows if o.get("status")=="CLOSED" and o.get("pnl") is not None]
    wins=sum(float(o["pnl"])>0 for o in closed)
    gross=sum(float(o["pnl"]) for o in closed)
    open_pnl=sum(float(o["pnl"] or 0) for o in filled if o.get("status") in ("OPEN","CLOSING"))
    strategies={}
    for name in sorted(set(o.get("strategy") or "unknown" for o in rows)):
        group=[o for o in rows if (o.get("strategy") or "unknown")==name]
        c=[o for o in group if o.get("status")=="CLOSED" and o.get("pnl") is not None]
        strategies[name]={"closed":len(c),"wins":sum(float(o["pnl"])>0 for o in c),"realized_usd":round(sum(float(o["pnl"]) for o in c),2),
                          "assessment":"INSUFFICIENT_TRADES" if len(c)<30 else ("REVIEW_UNDERPERFORMANCE" if sum(float(o["pnl"]) for o in c)<=0 else "NEEDS_BENCHMARK_TEST")}
    return {"orders":len(rows),"filled":len(filled),"closed_with_pnl":len(closed),"wins":wins,
            "win_rate_pct":round(100*wins/len(closed),2) if closed else None,
            "realized_usd":round(gross,2),"open_last_seen_usd":round(open_pnl,2),
            "net_after_costs_usd":None,"max_drawdown_pct":None,
            "note":"Closed order P&L is broker-reported where available; fees, external flows and a time-series equity curve are not reconciled here. Do not infer net profit or drawdown.",
            "strategies":strategies}

def alerts(articles,held_symbols=()):
    symbols=set(held_symbols)
    results=[]
    for article in articles:
        title=article.get("title","")
        if not CRITICAL.search(title):continue
        matched=[ticker for ticker,pattern in TICKERS.items() if re.search(pattern,title,re.I)]
        if not matched:continue
        results.append({"symbols":matched,"held":bool(symbols.intersection(matched)),"title":title,
                        "domain":article.get("domain"),"url":article.get("url"),
                        "first_seen_at":article.get("first_seen_at"),
                        "verification":"UNVERIFIED_HEADLINE","action":"MANUAL_REVIEW_ONLY"})
    # Repeated syndicated reports are not independent corroboration.
    return results[:50]
