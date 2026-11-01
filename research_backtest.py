"""Chronological, research-only backtests. No broker client or order endpoint."""
import math
from datetime import datetime, timedelta, timezone
import numpy as np
from research_strategies import *
from data_quality import utc_timestamp

PROFILES=("underlying_base","cfd_base","cfd_stress")
RULES={
 MEAN:"ETF long-only: z20 < -2, simple RSI14 <35, own close and SPY above SMA200. Next-open entry, stop signal close-2ATR14, target signal SMA20, exit at mean recovery or after10 sessions.",
 PAIRS_NAME:"Six predefined pairs; quarterly Engle-Granger on126 aligned log-price sessions, p<0.05/6, positive beta0.25..4 and half-life1..30. Enter2<=|z60|<3 at next open. Exit|z|<=0.5, |z|>=3.5, loss threshold, failed new calibration, or20 sessions. Both legs CFD, full gross cash reserved.",
 NEURAL:"Pooled ETF features; 8 tanh hidden units, fixed seed42, L2. Quarterly train on at most4500 labels ending before a five-session embargo. Predict t+1 open to t+5 close return>20bp. Long if p>=0.60 and close>SMA50; stop2ATR, target2R, exit p<0.45 or after5 sessions.",
}
ASSUMPTIONS=[
 "Research only. Three independent simulated accounts per cost profile/window; this is not allocation of three times the actual capital.",
 "Fixed present-day universe and retrospective strategy design create selection/survivorship bias. Corporate-action adjustments in broker candles are not independently verified.",
 "Prior completed daily candles only. Entry at next available open; long stop-first if both barriers touch; gap stop uses worse open.",
 "Underlying ETF profile: zero broker commission, 5bp adverse slippage per side. This requires the underlying ETF to be available for the account.",
 "CFD base: ETF leg15bp per side, GOLD leg20bp, 10bp execution slippage, assumed funding2bp per calendar day on gross notional.",
 "CFD stress: doubles CFD percentage charges, slippage20bp, funding5bp/day. No fixed stock commission is added to CFD charges.",
 "Regression hedge weights do not guarantee dollar, sector or market-beta neutrality.",
 "Neural outputs are model scores for a fixed 20bp label threshold, not calibrated probabilities of net profit after all scenario costs.",
 "Pairs always use the CFD product assumptions in every profile, require both legs and reserve full gross cash. No reuse of short-sale proceeds.",
 "Pair signals and risk exits use observed daily closes and execute next open. No intraday spread stop, guaranteed hedge, borrow availability or atomic two-leg execution is assumed.",
 "Pair entry reserves at most10% equity gross; single positions at most5%, with approximate entry risk0.5%. Minimum modeled leg notional USD10; actual product minimums may differ.",
 "All prices/funding/borrow charges are scenarios, not reconstructed broker trade-ticket costs. Taxes, dividends, cash interest and historical FX conversion are excluded.",
 "GBP uses one frozen broker GBPUSD close. Currency risk and conversion charges are excluded.",
 "Monthly returns are before simulated withdrawals. Withdraw at most GBP25 from positive monthly profits, available cash, cumulative closed-trade profits after previous withdrawals, and equity above original capital. Open/unrealized profits and losing months cannot fund a withdrawal.",
 "Partial calendar months are labeled and excluded from monthly target counts. Mark-to-market losses count in drawdown; withdrawals are added back for economic wealth/drawdown.",
 "The earlier/latest windows start with fresh GBP1250. Neural-network training may use only earlier observations/fully matured labels, including those before a window.",
 "Model features, parameters, six pairs and thresholds were fixed before this run; no search for the best-performing setting is done.",
]

def month_of(stamp): return stamp[:7]
def costs(profile,symbol,pair=False):
    cfd=pair or profile!="underlying_base"
    stress=profile=="cfd_stress"
    fee=((.002 if symbol=="GOLD" else .0015)*(2 if stress else 1)) if cfd else 0
    slip=.002 if stress else (.001 if cfd else .0005)
    carry=(.0005 if stress else .0002) if cfd else 0
    return fee,slip,carry

class Book:
    def __init__(self,strategy,profile,capital,fx):
        self.strategy,self.profile,self.initial,self.fx=strategy,profile,float(capital),float(fx)
        self.cash=float(capital); self.positions={}; self.last={}; self.trades=[]; self.curve=[]
        self.months=[]; self.month_start=float(capital); self.withdrawals=0; self.costs=0
    def equity(self):
        value=self.cash
        for p in self.positions.values():
            pnl=0; closing_cost=0
            for leg in p["legs"]:
                raw=self.last.get(leg["symbol"],leg["entry"])
                fee,slip,_=costs(self.profile,leg["symbol"],p["pair"])
                execution=raw*(1-leg["side"]*slip)
                pnl+=leg["side"]*leg["units"]*(execution-leg["entry"])
                closing_cost+=leg["units"]*execution*fee
            value+=p["gross"]+pnl-closing_cost
        return value
    def group_pnl(self,p):
        return sum(l["side"]*l["units"]*(self.last.get(l["symbol"],l["entry"])-l["entry"]) for l in p["legs"])
    def close(self,key,prices,stamp,reason):
        p=self.positions.pop(key)
        pnl=0; exit_fees=0
        for leg in p["legs"]:
            raw=prices[leg["symbol"]]
            fee,slip,_=costs(self.profile,leg["symbol"],p["pair"])
            execution=raw*(1-leg["side"]*slip)
            pnl+=leg["side"]*leg["units"]*(execution-leg["entry"])
            exit_fees+=leg["units"]*execution*fee
            self.costs+=leg["units"]*raw*slip
        self.cash+=p["gross"]+pnl-exit_fees
        self.costs+=exit_fees
        net=pnl-exit_fees-p["entry_fees"]-p["funding"]
        self.trades.append({"strategy":self.strategy,"instrument":key,"opened":p["opened"],"closed":stamp,
            "gross_usd":p["gross"],"net_pnl_usd":net,"net_pnl_gbp":net/self.fx,"reason":reason,
            "entry_fees_usd":p["entry_fees"],"exit_fees_usd":exit_fees,"funding_usd":p["funding"],
            "legs":[{k:v for k,v in l.items()} for l in p["legs"]]})
    def enter(self,key,signal,bars,stamp):
        signal_end=utc_timestamp(signal.get("signal_date"))
        day=utc_timestamp(stamp)
        if signal_end is None or signal_end+timedelta(days=1)>day or (day-signal_end).total_seconds()>4*86400:
            return
        pair=signal["strategy"]==PAIRS_NAME
        equity=max(0,self.equity())
        if pair:
            a,b=signal["a"],signal["b"]; beta=signal["beta"]
            if a not in bars or b not in bars: return
            sides=(-1,1) if signal["z"]>0 else (1,-1)
            allocation=[(a,sides[0],1/(1+beta)),(b,sides[1],beta/(1+beta))]
            risk_gap=(3.5-abs(signal["z"]))*signal["sd"]/(1+beta)
            if risk_gap<=0: return
            gross=min(equity*.10,equity*.005/risk_gap)
        else:
            symbol=key
            if symbol not in bars: return
            _,slip,_=costs(self.profile,symbol)
            price=bars[symbol]["open"]*(1+slip)
            gap=(price-signal["stop"])/price
            if not 0<signal["stop"]<price<signal["target"] or not 0<gap<=.08: return
            gross=min(equity*.05,equity*.005/gap)
            allocation=[(symbol,1,1)]
        held={l["symbol"] for p in self.positions.values() for l in p["legs"]}
        if any(s in held for s,_,_ in allocation): return
        total_rate=sum(w*costs(self.profile,s,pair)[0] for s,_,w in allocation)
        gross=min(gross,max(0,self.cash/(1+total_rate)))
        if any(gross*w<10 for _,_,w in allocation): return
        legs=[]; entry_fees=0
        for symbol,side,weight in allocation:
            fee,slip,_=costs(self.profile,symbol,pair)
            raw=bars[symbol]["open"]; execution=raw*(1+side*slip)
            notional=gross*weight; units=notional/execution
            legs.append({"symbol":symbol,"side":side,"units":units,"entry":execution,"notional":notional})
            entry_fees+=notional*fee
            self.costs+=units*raw*slip
            self.last[symbol]=raw
        self.cash-=gross+entry_fees; self.costs+=entry_fees
        self.positions[key]={"pair":pair,"legs":legs,"gross":gross,"entry_fees":entry_fees,"funding":0,
            "opened":stamp,"funded_to":stamp,"sessions":1,"risk_usd":equity*.005,"signal":dict(signal)}
    def step(self,stamp,bars,signals,feature_map,probabilities,valid_pairs):
        day=utc_timestamp(stamp)
        for s,b in bars.items(): self.last[s]=b["open"]
        exited=set()
        for key,p in list(self.positions.items()):
            days=max(0,(day-utc_timestamp(p["funded_to"])).total_seconds()/86400)
            funding=sum(l["notional"]*costs(self.profile,l["symbol"],p["pair"])[2]*days for l in p["legs"])
            self.cash-=funding; p["funding"]+=funding; self.costs+=funding; p["funded_to"]=stamp
            if not all(l["symbol"] in bars for l in p["legs"]): continue
            reason=None; prices={l["symbol"]:bars[l["symbol"]]["open"] for l in p["legs"]}
            if p["pair"]:
                sig=p["signal"]; a,b=sig["a"],sig["b"]
                fa,fb=feature_map.get(a),feature_map.get(b)
                if fa and fb:
                    z=(math.log(fa["close"])-sig["alpha"]-sig["beta"]*math.log(fb["close"])-sig["mu"])/sig["sd"]
                    # Prior-close loss observation, next-open execution.
                    previous_pnl=sum(l["side"]*l["units"]*((fa if l["symbol"]==a else fb)["close"]-l["entry"]) for l in p["legs"])
                    if abs(z)<=.5: reason="spread_reverted"
                    elif abs(z)>=3.5 or previous_pnl<=-p["risk_usd"]: reason="spread_risk_exit"
                if key not in valid_pairs: reason="cointegration_filter_failed"
            else:
                sig=p["signal"]; symbol=p["legs"][0]["symbol"]; b=bars[symbol]; f=feature_map.get(symbol)
                if b["open"]<=sig["stop"]: reason="gap_stop"
                elif b["open"]>=sig["target"]:
                    reason="gap_target"; prices[symbol]=sig["target"]
                elif self.strategy==MEAN and f and f["z"]>=0: reason="mean_recovered"
                elif self.strategy==NEURAL and probabilities.get(symbol,1)<.45: reason="probability_exit"
            if p["sessions"]>=p["signal"]["max_sessions"]: reason=reason or "time_exit"
            if reason:
                self.close(key,prices,stamp,reason); exited.add(key)
            else: p["sessions"]+=1
        for key,sig in signals.items():
            if key not in self.positions and key not in exited: self.enter(key,sig,bars,stamp)
        for key,p in list(self.positions.items()):
            if p["pair"]: continue
            s=p["legs"][0]["symbol"]
            if s not in bars: continue
            b=bars[s]; sig=p["signal"]
            if b["low"]<=sig["stop"]: self.close(key,{s:sig["stop"]},stamp,"stop")
            elif b["high"]>=sig["target"]: self.close(key,{s:sig["target"]},stamp,"target")
        for s,b in bars.items(): self.last[s]=b["close"]
        self.curve.append({"date":stamp,"equity_gbp":self.equity()/self.fx,
                           "economic_wealth_gbp":(self.equity()+self.withdrawals)/self.fx})
    def month_end(self,stamp,partial=False):
        value=self.equity(); profit=value-self.month_start
        realized_available=max(0,sum(t["net_pnl_usd"] for t in self.trades)-self.withdrawals)
        payout=min(25*self.fx,max(0,profit),max(0,value-self.initial),max(0,self.cash),realized_available)
        self.cash-=payout; self.withdrawals+=payout
        row={"month":month_of(stamp),"return_pct":profit/self.month_start*100 if self.month_start>0 else 0,
            "profit_gbp":profit/self.fx,"withdrawal_gbp":payout/self.fx,
            "closing_balance_gbp":self.equity()/self.fx,"partial":bool(partial)}
        self.months.append(row); self.month_start=self.equity()
        if self.curve:
            self.curve[-1].update(equity_gbp=self.equity()/self.fx,
                                 economic_wealth_gbp=(self.equity()+self.withdrawals)/self.fx)
    def finish(self,stamp):
        for key,p in list(self.positions.items()):
            self.close(key,{l["symbol"]:self.last[l["symbol"]] for l in p["legs"]},stamp,"end_of_window")
        if self.curve:
            self.curve[-1].update(equity_gbp=self.equity()/self.fx,
                                 economic_wealth_gbp=(self.equity()+self.withdrawals)/self.fx)
    def summary(self):
        pnl=np.array([t["net_pnl_usd"] for t in self.trades])
        peak=self.initial/self.fx; drawdown=0
        for row in self.curve:
            peak=max(peak,row["economic_wealth_gbp"])
            drawdown=max(drawdown,(peak-row["economic_wealth_gbp"])/peak*100)
        full=[m for m in self.months if not m["partial"]]
        gains=float(pnl[pnl>0].sum()); losses=float(-pnl[pnl<0].sum())
        wealth=self.cash+self.withdrawals
        return {"strategy":self.strategy,"profile":self.profile,"initial_gbp":self.initial/self.fx,
            "final_gbp":round(self.cash/self.fx,2),"withdrawn_gbp":round(self.withdrawals/self.fx,2),
            "economic_profit_gbp":round((wealth-self.initial)/self.fx,2),
            "total_return_pct":round((wealth/self.initial-1)*100,3),"max_drawdown_pct":round(drawdown,3),
            "trades":len(self.trades),"win_rate_pct":round(float(np.mean(pnl>0))*100,2) if len(pnl) else None,
            "profit_factor":round(gains/losses,3) if losses else None,
            "costs_gbp":round(self.costs/self.fx,2),"full_months":len(full),
            "months_at_least_10pct":sum(m["return_pct"]>=10 for m in full),
            "months_paid_25gbp":sum(m["withdrawal_gbp"]>=25-1e-8 for m in full),
            "losing_months":sum(m["return_pct"]<0 for m in full),
            "average_month_pct":round(float(np.mean([m["return_pct"] for m in full])),3) if full else None,
            "worst_month_pct":round(min(m["return_pct"] for m in full),3) if full else None}

def replay_research(data,capital_gbp=1250,progress=None):
    fx_rows=data.get("GBPUSD") or []
    if not fx_rows: raise ValueError("GBPUSD conversion data missing")
    fx=float(fx_rows[-1]["close"]); capital=capital_gbp*fx
    if not math.isfinite(capital) or capital<=0: raise ValueError("Invalid capital")
    master=sorted(r["fromDate"] for r in data.get("SPY",[]))
    if len(master)<500: raise ValueError("Need 500 SPY sessions for warmup and evaluation")
    dates=master[252:]
    # Start at the next complete month after the initial warmup session.
    dates=[d for d in dates if month_of(d)>month_of(dates[0])]
    months=sorted({month_of(d) for d in dates})
    split_month=months[max(1,int(len(months)*.75))]
    cutoff=next(d for d in dates if month_of(d)==split_month)
    datasets={s:rows for s,rows in data.items() if s in set(ETF_UNIVERSE)|{x for p in PAIRS for x in p}}
    schedule={}
    for s,rows in datasets.items():
        for b in rows: schedule.setdefault(b["fromDate"],{})[s]=b
    histories={s:[] for s in datasets}; pointers={s:0 for s in datasets}
    records=labeled_samples(datasets)
    label_lookup={(r["symbol"],r["feature_date"]):r for r in records}
    books={(w,mode,profile):Book(mode,profile,capital,fx)
           for w in ("earlier_75pct","latest_25pct") for mode in MODES for profile in PROFILES}
    models={}; fold=None; neural=None; folds=[]; pair_tests=[]; diagnostics=[]
    for i,stamp in enumerate(dates):
        # Rebuild prior-only histories for every symbol; no future OHLC enters a feature.
        for s,rows in datasets.items():
            j=pointers[s]
            while j<len(rows) and utc_timestamp(rows[j]["fromDate"])+timedelta(days=1)<=utc_timestamp(stamp): j+=1
            pointers[s]=j; histories[s]=rows[max(0,j-252):j]
        current=schedule.get(stamp,{})
        fmap={s:features(rows) for s,rows in histories.items()}
        quarter=(stamp[:4],(int(stamp[5:7])-1)//3)
        if quarter!=fold:
            fold=quarter
            # Five complete master sessions between the latest training-label end and prediction.
            full_index=master.index(stamp)
            embargo=master[max(0,full_index-5)]
            neural,info=fit_walkforward(records,embargo)
            folds.append({"prediction_from":stamp,**info})
            models={}
            for a,b in PAIRS:
                model,info=calibrate_pair(histories,a,b)
                key=a+"/"+b
                pair_tests.append({"fold_from":stamp,"pair":key,"accepted":model is not None,**info})
                if model: models[key]=model
        probabilities={}
        if neural:
            symbols=[s for s in ETF_UNIVERSE if s in current and fmap.get(s)]
            if symbols:
                predictions=neural.predict([fmap[s]["x"] for s in symbols])
                probabilities=dict(zip(symbols,map(float,predictions)))
                for s,p in probabilities.items():
                    label=label_lookup.get((s,fmap[s]["date"]))
                    if label:
                        diagnostics.append({"date":stamp,"symbol":s,"p":p,"y":label["y"],
                                            "baseline":neural.majority})
        signals={mode:{} for mode in MODES}
        for s in ETF_UNIVERSE:
            if s not in current: continue
            mr=mean_reversion_signal(fmap.get(s),fmap.get("SPY"))
            nn=neural_signal(fmap.get(s),probabilities.get(s))
            if mr: signals[MEAN][s]=mr
            if nn: signals[NEURAL][s]=nn
        for key,model in models.items():
            sig=pair_signal(histories,model)
            if sig: signals[PAIRS_NAME][key]=sig
        window="earlier_75pct" if stamp<cutoff else "latest_25pct"
        last_of_window=(i==len(dates)-1 or (i+1<len(dates) and dates[i+1]==cutoff))
        month_end=(i==len(dates)-1 or month_of(dates[i+1])!=month_of(stamp))
        for (w,mode,profile),book in books.items():
            if w!=window: continue
            book.step(stamp,current,signals[mode],fmap,probabilities,set(models))
            if last_of_window: book.finish(stamp)
            if month_end:
                # Dataset ends can be mid-month; mark that month as partial.
                next_month=(utc_timestamp(stamp).replace(day=28)+timedelta(days=4)).replace(day=1)
                remaining_business=np.busday_count((utc_timestamp(stamp)+timedelta(days=1)).date(),next_month.date())
                partial=i==len(dates)-1 and remaining_business>0
                book.month_end(stamp,partial)
        if progress and (i%100==0 or i==len(dates)-1): progress(i+1,len(dates))
    summaries=[]; curves={}; trades={}; monthly={}
    for (window,mode,profile),book in books.items():
        key="|".join((window,mode,profile))
        summaries.append({**book.summary(),"window":window,
            "from":book.curve[0]["date"],"to":book.curve[-1]["date"]})
        curves[key]=book.curve; trades[key]=book.trades; monthly[key]=book.months
    evaluation={}
    for window in ("earlier_75pct","latest_25pct"):
        obs=[o for o in diagnostics if (o["date"]<cutoff)==(window=="earlier_75pct")]
        evaluation[window]={"predictions":len(obs),
            "direction_accuracy_pct":float(np.mean([(o["p"]>=.5)==bool(o["y"]) for o in obs]))*100 if obs else None,
            "majority_baseline_accuracy_pct":float(np.mean([o["baseline"]==o["y"] for o in obs]))*100 if obs else None,
            "brier_score":float(np.mean([(o["p"]-o["y"])**2 for o in obs])) if obs else None}
    benchmarks=[]
    for window in ("earlier_75pct","latest_25pct"):
        selected=[r for r in data["SPY"] if r["fromDate"] in dates and
                  (r["fromDate"]<cutoff)==(window=="earlier_75pct")]
        for profile in PROFILES:
            fee,slip,_=costs(profile,"SPY")
            entry=selected[0]["open"]*(1+slip)
            units=capital/(entry*(1+fee))
            final=units*selected[-1]["close"]*(1-slip)*(1-fee)
            benchmarks.append({"window":window,"profile":profile,"strategy":"SPY buy-and-hold",
                "initial_gbp":capital_gbp,"final_gbp":round(final/fx,2),
                "profit_gbp":round((final-capital)/fx,2),
                "return_pct":round((final/capital-1)*100,3),
                "note":"Fully invested; no monthly withdrawals"})
    return {"summaries":summaries,"curves":curves,"trades":trades,"monthly":monthly,
        "rules":RULES,"assumptions":ASSUMPTIONS,"folds":folds,"pair_tests":pair_tests,
        "neural_diagnostics":evaluation,"split_date":cutoff,
        "initial_gbp":capital_gbp,"gbpusd":fx,"fx_date":fx_rows[-1]["fromDate"],
        "coverage":{s:{"bars":len(r),"from":r[0]["fromDate"],"to":r[-1]["fromDate"]} for s,r in datasets.items() if r},
        "benchmarks":benchmarks}
