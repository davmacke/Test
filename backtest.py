"""Daily-bar strategy replay. Read-only: this module has no broker order access."""
import math
from datetime import datetime, timedelta, timezone
from statistics import mean
from analysis import analyze_candles
from bollinger_rsi import analyze_bollinger_rsi, plan_bollinger_entry, STRATEGY as BB
from sector_rotation import analyze_rotation, plan_rotation_entry, STRATEGY as ROTATION, SUPPORTED as ROTATION_SYMBOLS
from data_quality import utc_timestamp

MODES = ("trend", BB, ROTATION, "combined")
SCENARIOS = {
    "base": {"fee_multiplier": 1, "slippage_bps": 5, "funding_bps_per_day": 2},
    "stress": {"fee_multiplier": 2, "slippage_bps": 20, "funding_bps_per_day": 5},
}
BASE_FEES = {"Crypto": 100, "Stocks": 15, "ETFs": 15, "Commodities": 20,
             "Currencies": 5, "Indices": 10}
CARRY_CLASSES = {"Commodities", "Currencies", "Indices"}
ASSUMPTIONS = [
    "Strategy-level replay on a fixed present-day universe: survivorship and selection bias remain.",
    "Signals use only prior completed daily candles, up to 100 bars to match live analysis.",
    "Entry and signal exits occur at the next available daily candle open; broker flags and historical bid/ask are unavailable.",
    "Daily candle boundaries are broker-defined and can differ from exchange sessions.",
    "Stops fill at the open if gapped through. If stop and target both touch in one bar, stop wins.",
    "Current strategy sizing: 0.5% account risk, at most 5% allocation per position, long only, 1x.",
    "Cost scenarios are assumptions, not reconstructed eToro fees: base per-side bps Crypto100/Stocks15/ETFs15/Commodities20/FX5/Indices10; stress doubles them.",
    "Additional adverse execution slippage: base5/stress20 bps per side. Stocks add USD2 commission per side.",
    "Assumed funding for commodities/FX/indices: base2/stress5 bps of entry notional per calendar day; actual broker charges can differ.",
    "No dividends, taxes, FX conversion costs, cash interest, delisted instruments, or historical trading restrictions are reconstructed.",
    "Replay permits later re-entry after exit. Live run expiry, one-attempt-per-symbol run bookkeeping and current pre-existing holdings are not simulated.",
    "The latest 25% of replay dates are reported separately with fresh simulated capital; parameters are not optimized on these data.",
    "This evaluation is retrospective: strategy and universe choices used current market knowledge, so it is not untouched prospective validation.",
    "Final positions are liquidated at last available close with exit costs. A buy-and-hold SPY comparator is fully invested, unlike strategy cash balances.",
]
def fees(category, scenario):
    cfg=SCENARIOS[scenario]
    return BASE_FEES.get(category,20)*cfg["fee_multiplier"]/10000, (2.0 if category=="Stocks" else 0.0)

def choose(mode, signals):
    if mode=="trend": return "trend",signals.get("trend") or {}
    if mode==BB: return BB,signals.get(BB) or {}
    if mode==ROTATION: return ROTATION,signals.get(ROTATION) or {}
    bb=signals.get(BB) or {}
    if bb.get("action")=="BUY CANDIDATE": return BB,bb
    if ROTATION in signals: return ROTATION,signals[ROTATION]
    return "trend",signals.get("trend") or {}

class Account:
    def __init__(self, mode, scenario, capital=10000):
        self.mode,self.scenario,self.initial=mode,scenario,float(capital)
        self.cash=float(capital)
        self.positions={}
        self.trades=[]
        self.curve=[]
        self.last={}
        self.costs=0.0
    def equity(self):
        return self.cash+sum(p["units"]*self.last.get(s,p["entry"]) for s,p in self.positions.items())
    def close(self,symbol,price,stamp,reason):
        p=self.positions.pop(symbol)
        rate,fixed=fees(p["category"],self.scenario)
        execution=price*(1-SCENARIOS[self.scenario]["slippage_bps"]/10000)
        proceeds=p["units"]*execution
        fee=proceeds*rate+fixed
        self.cash+=proceeds-fee
        self.costs+=fee+p["units"]*(price-execution)
        net=proceeds-fee-p["notional"]-p["entry_fee"]-p["funding"]
        self.trades.append({"symbol":symbol,"strategy":p["strategy"],"opened":p["opened"],"closed":stamp,
            "entry":p["entry"],"exit":execution,"notional_usd":p["notional"],"net_pnl_usd":net,
            "reason":reason,"funding_usd":p["funding"]})
    def step(self,stamp,bars,signals,classes):
        day=utc_timestamp(stamp)
        exited=set()
        for symbol,p in list(self.positions.items()):
            delta=max(0,(day-utc_timestamp(p["funded_to"])).total_seconds()/86400)
            if p["category"] in CARRY_CLASSES:
                funding=p["notional"]*SCENARIOS[self.scenario]["funding_bps_per_day"]/10000*delta
                self.cash-=funding; p["funding"]+=funding; self.costs+=funding
            p["funded_to"]=stamp
            bar=bars.get(symbol)
            if bar is None: continue
            self.last[symbol]=bar["open"]
            own=(signals.get(symbol) or {}).get(p["strategy"]) or {}
            if bar["open"]<=p["stop"]:
                self.close(symbol,bar["open"],stamp,"gap_stop"); exited.add(symbol)
            elif bar["open"]>=p["target"]:
                self.close(symbol,p["target"],stamp,"gap_target"); exited.add(symbol)
            elif p["strategy"]!="bollinger_rsi_v1" and own.get("action")=="REDUCE / AVOID":
                self.close(symbol,bar["open"],stamp,"signal_exit"); exited.add(symbol)
        for symbol,bar in bars.items():
            if symbol in self.positions or symbol in exited: continue
            strategy,signal=choose(self.mode,signals.get(symbol) or {})
            if signal.get("action")!="BUY CANDIDATE": continue
            category=classes.get(symbol,"Unclassified")
            price=bar["open"]*(1+SCENARIOS[self.scenario]["slippage_bps"]/10000)
            equity=max(0,self.equity())
            if strategy in {BB,ROTATION}:
                planner=plan_bollinger_entry if strategy==BB else plan_rotation_entry
                plan,reason=planner(signal,price,equity,day)
                if not plan: continue
                stop,target,gap=plan["stop_rate"],plan["target_rate"],plan["stop_gap_pct"]
            else:
                gap=signal.get("stop_gap_pct") or 0
                stop,target=price*(1-gap/100),price*(1+2*gap/100)
            if not 0<gap<=8 or not 0<stop<price<target: continue
            rate,fixed=fees(category,self.scenario)
            amount=min(equity*.05,equity*.005/(gap/100),max(0,(self.cash-fixed)/(1+rate)))
            if amount<10: continue
            fee=amount*rate+fixed
            self.cash-=amount+fee
            self.costs+=fee+amount/price*(price-bar["open"])
            self.positions[symbol]={"strategy":strategy,"units":amount/price,"entry":price,
                "stop":stop,"target":target,"notional":amount,"entry_fee":fee,"funding":0,
                "category":category,"opened":stamp,"funded_to":stamp}
            self.last[symbol]=price
        for symbol,p in list(self.positions.items()):
            bar=bars.get(symbol)
            if bar is None: continue
            # New entries and existing holdings use the same pessimistic intrabar ordering.
            if bar["low"]<=p["stop"]: self.close(symbol,p["stop"],stamp,"stop")
            elif bar["high"]>=p["target"]: self.close(symbol,p["target"],stamp,"target")
            self.last[symbol]=bar["close"]
        self.curve.append({"date":stamp,"equity":self.equity()})
    def finish(self,stamp):
        for symbol in list(self.positions):
            self.close(symbol,self.last[symbol],stamp,"end_of_window")
        if self.curve: self.curve[-1]["equity"]=self.cash
        peak=self.initial; drawdown=0
        for row in self.curve:
            peak=max(peak,row["equity"])
            drawdown=max(drawdown,(peak-row["equity"])/peak*100)
        pnls=[t["net_pnl_usd"] for t in self.trades]
        gains=sum(x for x in pnls if x>0); losses=-sum(x for x in pnls if x<0)
        weekly={}
        for t in self.trades:
            stamp=utc_timestamp(t["closed"])
            year,week,_=stamp.isocalendar()
            key=f"{year}-W{week:02d}"
            weekly[key]=weekly.get(key,0)+t["net_pnl_usd"]
        summary={"strategy":self.mode,"scenario":self.scenario,"initial_usd":self.initial,
            "final_usd":round(self.cash,2),"net_pnl_usd":round(self.cash-self.initial,2),
            "return_pct":round((self.cash/self.initial-1)*100,3),"max_drawdown_pct":round(drawdown,3),
            "trades":len(pnls),"win_rate_pct":round(sum(x>0 for x in pnls)/len(pnls)*100,2) if pnls else None,
            "profit_factor":round(gains/losses,3) if losses else None,
            "no_losing_trades":bool(pnls) and losses==0,
            "modeled_costs_usd":round(self.costs,2)}
        # Include no-trade weeks: otherwise sparse strategies overstate weekly income.
        weeks={}
        for row in self.curve:
            y,w,_=utc_timestamp(row["date"]).isocalendar(); weeks[f"{y}-W{w:02d}"]=0
        summary["realized_weekly_pnl_usd"]={w:round(weekly.get(w,0),2) for w in weeks}
        return summary

def replay(data,classes,capital=10000,progress=None):
    clean={s:sorted(rows,key=lambda b:b["fromDate"]) for s,rows in data.items() if len(rows)>=51}
    dates=sorted({r["fromDate"] for rows in clean.values() for r in rows})
    if len(dates)<80: raise ValueError("Need at least 80 daily dates with 51-bar instrument histories")
    # First 50 dates warm up; instruments with shorter histories warm up independently.
    dates=dates[50:]
    split=max(1,int(len(dates)*.75))
    cutoff=dates[min(split,len(dates)-1)]
    histories={s:[] for s in clean}
    schedule={}
    for s,rows in clean.items():
        for b in rows: schedule.setdefault(b["fromDate"],{})[s]=b
    for stamp in sorted(schedule):
        if stamp>=dates[0]: break
        for s,b in schedule[stamp].items(): histories[s].append(b)
    books={(window,mode,scenario):Account(mode,scenario,capital)
           for window in ("earlier_75pct","latest_25pct") for mode in MODES for scenario in SCENARIOS}
    summaries=[]; curves={}; trades={}
    for index,stamp in enumerate(dates):
        day=utc_timestamp(stamp)
        bars=schedule[stamp]
        now=day.isoformat()
        rotation=analyze_rotation({s:{"bars":rows[-100:],"evaluated_at":now}
                                  for s,rows in histories.items() if s in (*ROTATION_SYMBOLS, "SPY")},capital,day)
        signals={}
        for s,bar in bars.items():
            past=histories[s][-100:]
            if len(past)<50: continue
            signals[s]={"trend":analyze_candles(past,current_price=bar["open"],equity=capital),
                        BB:analyze_bollinger_rsi(past,current_price=bar["open"],equity=capital,now=day)}
            if s in rotation: signals[s][ROTATION]=rotation[s]
        window="earlier_75pct" if stamp<cutoff else "latest_25pct"
        for (w,mode,scenario),book in books.items():
            if w==window: book.step(stamp,bars,signals,classes)
        for s,b in bars.items(): histories[s].append(b)
        if index==split-1 or index==len(dates)-1:
            for (w,mode,scenario),book in books.items():
                if w!=window: continue
                summary=book.finish(stamp); summary["window"]=w
                summary["from"]=book.curve[0]["date"] if book.curve else None; summary["to"]=stamp
                summaries.append(summary)
                key="|".join((w,mode,scenario))
                curves[key]=book.curve; trades[key]=book.trades
        if progress and (index%50==0 or index==len(dates)-1):
            progress(index+1,len(dates))
    # SPY comparator over each exact strategy window, 100% invested.
    benchmarks=[]
    spy=clean.get("SPY") or []
    for window,start,end in (("earlier_75pct",dates[0],dates[split-1]),("latest_25pct",cutoff,dates[-1])):
        rows=[r for r in spy if start<=r["fromDate"]<=end]
        if not rows: continue
        for scenario in SCENARIOS:
            rate,_=fees("ETFs",scenario); slip=SCENARIOS[scenario]["slippage_bps"]/10000
            entry=rows[0]["open"]*(1+slip); units=capital/(entry*(1+rate))
            final=units*rows[-1]["close"]*(1-slip)*(1-rate)
            benchmarks.append({"window":window,"scenario":scenario,"strategy":"SPY buy-and-hold",
                "from":rows[0]["fromDate"],"to":rows[-1]["fromDate"],
                "net_pnl_usd":round(final-capital,2),"return_pct":round((final/capital-1)*100,3)})
    return {"summaries":summaries,"benchmarks":benchmarks,"curves":curves,"trades":trades,
        "split_date":cutoff,"assumptions":ASSUMPTIONS,"scenarios":SCENARIOS,
        "coverage":{s:{"bars":len(r),"from":r[0]["fromDate"],"to":r[-1]["fromDate"]} for s,r in clean.items()},
        "rejected_symbols":sorted(set(data)-set(clean))}


def sterling_capital(data, amount_gbp):
    """Constant conversion snapshot, not a historical GBP equity conversion."""
    rows=data.get("GBPUSD") or []
    if not rows: raise ValueError("GBPUSD history required for GBP capital")
    rate=float(rows[-1]["close"])
    amount=float(amount_gbp)
    if not math.isfinite(rate) or not math.isfinite(amount) or rate<=0 or amount<=0:
        raise ValueError("Invalid GBP capital or conversion")
    return amount*rate,rate,rows[-1]["fromDate"]

def add_sterling_comparison(report, capital_gbp, rate, rate_date, target_gbp=25):
    report["sterling"]={"initial_gbp":capital_gbp,"gbpusd":rate,"rate_date":rate_date,
        "target_gbp":target_gbp,"method":"Constant conversion using latest fetched GBPUSD daily close"}
    for result in report["summaries"]:
        result["net_pnl_gbp"]=round(result["net_pnl_usd"]/rate,2)
        result["final_gbp"]=round(result["final_usd"]/rate,2)
        weekly=list(result["realized_weekly_pnl_usd"].values())
        result["weeks_realized_at_least_target"]=sum(x/rate>=target_gbp for x in weekly)
        result["weeks_in_window"]=len(weekly)
    report["assumptions"].append("GBP display uses a single current broker GBPUSD snapshot. Historical GBP/USD changes and conversion charges are not modeled. Weekly realized P&L is lumpy and includes forced end-of-window liquidation; target-hit counts do not forecast income.")
    return report
