"""Public-disclosure followers. Pure simulation: no broker or trading imports."""
from bisect import bisect_right
from datetime import date
import math
import re

PELOSI="pelosi_household_stock_follower_v1"
BERKSHIRE="berkshire_public_13f_top10_v1"
BENCHMARK="spy_95pct_benchmark"
PROFILES={"underlying_zero":(0,0,.0005,0),
          "underlying_2usd":(2,0,.0005,0),
          "cfd_base":(0,.0015,.001,0),
          "cfd_stress":(0,.003,.002,.0002)}
RULES={PELOSI:"Ordinary stock purchases only; 10% equity per active name, total at most95%. Repeated purchase restores target rather than inferring unknown trade size. Any qualifying sale removes the entire copied name. Options, exercises, gifts, transfers and flagged records excluded. Initial undisclosed holdings unknown: start empty before the available transaction ledger.",
 BERKSHIRE:"Ten largest CUSIPs in each original public Berkshire 13F. Target95% times disclosed value/full public filing value; remainder cash. Aggregated manager rows, distinct share classes. No hindsight from confidential amendments. Original quarterly filings only; amendments are not followed.",
 BENCHMARK:"95% SPY at first window open, remaining cash, no rebalancing. Same cost and withdrawal rules."}
ASSUMPTIONS=[
 "Research only; no new demo or real broker orders. These are disclosure followers, not real-time copying or exact replications of the underlying portfolios.",
 "Public dataset cutoff7September2026; evaluation ends31August2026. This does not represent current holdings or a real-time disclosure feed. Earlier2024-25 and latestJan-Aug2026 are independent fresh-capital windows, not untouched out-of-sample validation.",
 "Execute strictly after filing date at next SPY session open, then optionally delay another5SPY sessions. Trade dates and quarter-end dates never trigger entries. Time-of-day unavailable for House filings; date-only conservative availability. Berkshire acceptance times retained for audit.",
 "Pelosi household reports predominantly concern spouse Paul. No options returns are replaced by underlying stock returns. Sales of shares not held in the copy account do nothing. Dollar ranges are retained for audit, never treated as exact weights.",
 "Berkshire filings cover a disclosed public securities subset, excluding its cash/private businesses and some foreign assets. This is a Berkshire follower associated with Buffett, not his personal trade record or attribution of each decision to him.",
 "Fractional long shares,1x exposure, no shorts or leverage. Missing ticker/open: its target stays cash, no renormalization. Rebalance sells before buys; target uses pre-rebalance equity. Minimum simulated order USD10; actual minimums/availability may differ.",
 "Underlying profiles: zero or USD2 per order plus5bp adverse slippage. CFD base:15bp per side,10bp slippage, zero assumed1x carry. Stress:30bp per side,20bp slippage,2bp per calendar day carry. Mutually exclusive product scenarios, not verified account fees. No fixed stock commissions added to CFDs.",
 "Frozen broker daily candles, corporate-action adjustments not independently verified; no dividends, tax, cash interest or historical currency conversion. Fixed GBPUSD from frozen dataset. Fees and funding are assumptions. Price gaps/stale marks disclosed; no future price backfill.",
 "Monthly GBP25 withdrawal requires positive month profit, equity above initial, cash and cumulative net realized profits after prior payouts. Partial months cannot trigger withdrawals/target counts. Monthly returns before withdrawals; economic profit includes payouts. Window-end liquidation is artificial and cannot manufacture a historical monthly payout.",
 "Strategies selected retrospectively; sparse signals, concentration and short windows limit conclusions. Historical gains do not establish a reliable10%monthly return."
]

def qualifying(row):
    if row.get("needsReview"): return False,"flagged"
    if not row.get("filedAt") or not row.get("transactedAt"): return False,"missing_date"
    try:
        filed=date.fromisoformat(row["filedAt"]); traded=date.fromisoformat(row["transactedAt"])
    except ValueError: return False,"invalid_date"
    if traded>filed: return False,"trade_after_filing"
    if row.get("assetType")!="stock": return False,"not_stock"
    if row.get("side") not in ("buy","sell"): return False,"not_market_side"
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}",row.get("ticker") or ""): return False,"no_ticker"
    if re.search(r"exercis|contribut|donor|charit|gift|spinoff|spin-off|transfer",row.get("assetDescription", ""),re.I):
        return False,"exercise_or_nonmarket"
    return True,"ordinary_stock"

def events(ledger,strategy,sessions,lag=0):
    """Map public dates to eligible sessions, preserving full public-value denominator."""
    if lag<0: raise ValueError("Negative disclosure delay")
    out=[]; audit=[]
    rows=ledger["pelosi"] if strategy==PELOSI else ledger["berkshire"]
    grouped={}
    for row in rows:
        if strategy==PELOSI:
            ok,reason=qualifying(row)
            audit.append({"id":row["id"],"included":ok,"reason":reason})
            if not ok: continue
            filed=row["filedAt"]
        else: filed=row["filed_at"]
        grouped.setdefault(filed,[]).append(row)
    active=set()
    for filed,group in sorted(grouped.items()):
        index=bisect_right(sessions,filed)+lag
        if index>=len(sessions): continue
        if strategy==PELOSI:
            # Apply transactions in historical transaction order; repeated names are idempotent.
            for row in sorted(group,key=lambda r:(r["transactedAt"],str(r["id"]))):
                if row["side"]=="buy": active.add(row["ticker"])
                else: active.discard(row["ticker"])
            weight=min(.10,.95/len(active)) if active else 0
            weights={s:weight for s in sorted(active)}
            sources=sorted({r["source_url"] for r in group})
        else:
            row=group[-1]; total=float(row["total_reported_value"])
            if total<=0: raise ValueError("Invalid public filing value")
            weights={}
            for h in sorted(row["holdings"],key=lambda h:h["value"],reverse=True)[:10]:
                s=h.get("ticker") or "UNMAPPED:"+h["cusip"]
                weights[s]=weights.get(s,0)+.95*float(h["value"])/total
            if sum(weights.values())>.95000001: raise ValueError("Holdings exceed public filing total")
            sources=[row["source_url"]]
        out.append({"filed":filed,"execute":sessions[index],"weights":weights,"sources":sources})
    return out,audit

class Account:
    def __init__(self,capital,fx,profile):
        if profile not in PROFILES or not math.isfinite(capital*fx) or min(capital,fx)<=0: raise ValueError("Invalid account")
        self.initial=capital*fx; self.fx=fx; self.profile=profile; self.cash=self.initial
        self.positions={}; self.last={}; self.last_date={}; self.realized=0.; self.withdrawn=0.; self.costs=0.
        self.month_start=self.initial; self.months=[]; self.curve=[]; self.orders=[]; self.rebalances=[]
        self.stale_held_days=0; self.previous_day=None
    def equity(self,liquidation=False):
        fixed,fee,slip,_=PROFILES[self.profile]
        return self.cash+sum(p["units"]*self.last[s]*(1-slip if liquidation else 1)*(1-fee if liquidation else 1)-(fixed if liquidation else 0) for s,p in self.positions.items())
    def trade(self,s,delta,raw,day,reason):
        fixed,fee,slip,_=PROFILES[self.profile]
        if abs(delta)*raw<10 and reason!="liquidation": return
        if delta>0:
            execution=raw*(1+slip)
            units=min(delta,max(0,(self.cash-fixed)/(execution*(1+fee))))
            if units*raw<10: return
            paid=units*execution; charge=paid*fee+fixed; total=paid+charge
            old=self.positions.get(s,{"units":0.,"basis":0.})
            self.positions[s]={"units":old["units"]+units,"basis":old["basis"]+total}
            self.cash-=total; net=None
        else:
            old=self.positions[s]; units=min(-delta,old["units"])
            execution=raw*(1-slip); paid=units*execution; charge=paid*fee+fixed
            basis=old["basis"]*units/old["units"]; net=paid-charge-basis
            self.cash+=paid-charge; self.realized+=net
            old["units"]-=units; old["basis"]-=basis
            if old["units"]<1e-10: self.positions.pop(s)
        self.costs+=charge+units*raw*slip
        self.orders.append({"date":day,"symbol":s,"side":"buy" if delta>0 else "sell","units":units,
                            "execution_usd":execution,"commission_usd":charge,"realized_gbp":None if net is None else net/self.fx,"reason":reason})
    def step(self,day,bars,event=None):
        if self.previous_day:
            days=(date.fromisoformat(day)-date.fromisoformat(self.previous_day)).days
            funding=sum(p["units"]*self.last[s] for s,p in self.positions.items())*PROFILES[self.profile][3]*days
            self.cash-=funding; self.realized-=funding; self.costs+=funding
        self.previous_day=day
        for s,b in bars.items(): self.last[s]=b["open"]; self.last_date[s]=day
        if event is not None:
            value=max(0,self.equity()); weights=event["weights"]
            missing={s:w for s,w in weights.items() if s not in bars}
            deltas={}
            for s in set(weights)|set(self.positions):
                if s not in bars: continue
                deltas[s]=value*weights.get(s,0)/bars[s]["open"]-self.positions.get(s,{}).get("units",0)
            for s,delta in sorted(deltas.items(),key=lambda x:x[1]>0):
                self.trade(s,delta,bars[s]["open"],day,"disclosure_rebalance")
            self.rebalances.append({**event,"executed":day,"missing_target_weights":missing,
                                    "missing_weight_pct":100*sum(missing.values()),"target_weight_pct":100*sum(weights.values())})
        for s,b in bars.items(): self.last[s]=b["close"]
        if any((date.fromisoformat(day)-date.fromisoformat(self.last_date[s])).days>7 for s in self.positions): self.stale_held_days+=1
        self.curve.append({"date":day,"equity_gbp":self.equity(True)/self.fx,"wealth_gbp":(self.equity(True)+self.withdrawn)/self.fx})
    def month_end(self,day,partial=False):
        value=self.equity(True); profit=value-self.month_start
        payout=0 if partial else min(25*self.fx,max(0,profit),max(0,value-self.initial),max(0,self.cash),max(0,self.realized-self.withdrawn))
        self.cash-=payout; self.withdrawn+=payout
        self.months.append({"month":day[:7],"return_pct":100*profit/self.month_start if self.month_start>0 else 0,
                            "profit_gbp":profit/self.fx,"withdrawal_gbp":payout/self.fx,"partial":bool(partial)})
        self.month_start=self.equity(True)
    def finish(self,day,bars):
        # Only current-session closes can liquidate; stale positions remain marked and flagged.
        for s,p in list(self.positions.items()):
            if s in bars: self.trade(s,-p["units"],bars[s]["close"],day,"liquidation")
    def summary(self):
        wealth=self.equity(True)+self.withdrawn; peak=self.initial; dd=0
        for r in self.curve:
            v=r["wealth_gbp"]*self.fx; peak=max(peak,v); dd=max(dd,100*(peak-v)/peak)
        full=[m for m in self.months if not m["partial"]]
        return {"initial_gbp":self.initial/self.fx,"final_gbp":round(self.equity(True)/self.fx,2),
                "economic_profit_gbp":round((wealth-self.initial)/self.fx,2),"total_return_pct":round(100*(wealth/self.initial-1),3),
                "withdrawn_gbp":round(self.withdrawn/self.fx,2),"costs_gbp":round(self.costs/self.fx,2),"orders":len(self.orders),
                "max_drawdown_pct":round(dd,3),"full_months":len(full),"months_at_least_10pct":sum(m["return_pct"]>=10 for m in full),
                "months_paid_25gbp":sum(m["withdrawal_gbp"]>=25-1e-8 for m in full),"losing_months":sum(m["return_pct"]<0 for m in full),
                "worst_month_pct":round(min((m["return_pct"] for m in full),default=0),3),
                "stale_held_days":self.stale_held_days,"unliquidated_symbols":sorted(self.positions),
                "max_missing_target_weight_pct":round(max((r["missing_weight_pct"] for r in self.rebalances),default=0),3)}

def replay_copy(data,ledger,capital_gbp=1250):
    # Deliberately fixed and visible research windows; no selecting the best observed interval.
    windows={"earlier_2024_2025":("2024-01-01","2025-12-31"),"latest_2026_jan_aug":("2026-01-01",ledger["test_end"])}
    schedule={}
    for s,rows in data.items():
        if s=="GBPUSD": continue
        for b in rows:
            d=b["fromDate"][:10]
            if all(math.isfinite(float(b[k])) and float(b[k])>0 for k in ("open","close")):
                schedule.setdefault(d,{})[s]=b
    sessions=sorted(d for d,b in schedule.items() if "SPY" in b)
    fxrows=data.get("GBPUSD") or []
    if not fxrows or not sessions: raise ValueError("Missing SPY or GBPUSD")
    fx=float(fxrows[-1]["close"])
    result={"rules":RULES,"assumptions":ASSUMPTIONS,"gbpusd":fx,"fx_date":fxrows[-1]["fromDate"],
            "disclosure_dataset_as_of":ledger["disclosure_dataset_as_of"],"test_end":ledger["test_end"],"summaries":[],"runs":[],"filing_sources":ledger}
    for strategy in (PELOSI,BERKSHIRE,BENCHMARK):
        for lag in (0,5):
            if strategy==BENCHMARK and lag: continue
            ev,audit=events(ledger,strategy,sessions,lag) if strategy!=BENCHMARK else ([],[])
            for window,(start,end) in windows.items():
                days=[d for d in sessions if start<=d<=end]
                if not days: continue
                # Latest PUBLIC target seeds each independent fresh-capital window.
                prior=[e for e in ev if e["execute"]<days[0]]
                by_day={e["execute"]:e for e in ev if days[0]<=e["execute"]<=days[-1]}
                if prior and days[0] not in by_day: by_day[days[0]]={**prior[-1],"seed_prior_disclosure":True}
                if strategy==BENCHMARK: by_day={days[0]:{"weights":{"SPY":.95},"filed":None,"execute":days[0],"sources":[]}}
                for profile in PROFILES:
                    book=Account(capital_gbp,fx,profile)
                    for i,d in enumerate(days):
                        book.step(d,schedule[d],by_day.get(d))
                        last=i==len(days)-1
                        if last: book.finish(d,schedule[d])
                        if last or days[i+1][:7]!=d[:7]:
                            # Compare to full known SPY calendar, not a truncated requested interval.
                            full=[x for x in sessions if x[:7]==d[:7]]
                            partial=(days[0][:7]==d[:7] and days[0]!=full[0]) or d!=full[-1]
                            # End liquidation is not a source of simulated month-end withdrawals.
                            if last:
                                realized=book.realized
                                book.realized-=sum(o["realized_gbp"]*fx for o in book.orders if o["reason"]=="liquidation" and o["realized_gbp"] is not None)
                                book.month_end(d,partial); book.realized=realized
                            else: book.month_end(d,partial)
                    label={"strategy":strategy,"window":window,"profile":profile,"extra_delay_sessions":lag,"start":days[0],"end":days[-1]}
                    summary={**label,**book.summary()}; result["summaries"].append(summary)
                    result["runs"].append({**label,"months":book.months,"curve":book.curve,"orders":book.orders,"rebalances":book.rebalances,"filter_audit":audit})
    return result
