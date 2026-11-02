"""Read-only portfolio evaluation mathematics. No broker or order API."""
from datetime import datetime,timezone
import math
import statistics as st

def stamp(s): return datetime.fromisoformat(s.replace("Z","+00:00"))
def variance(x): return st.variance(x) if len(x)>1 else 0.0
def covariance(a,b):
    if len(a)<2:return 0.0
    ma,mb=st.mean(a),st.mean(b)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/(len(a)-1)

def dietz(previous,current,flows):
    start,end=stamp(previous["stamp"]),stamp(current["stamp"])
    seconds=(end-start).total_seconds()
    if seconds<=0: raise ValueError("Observations must be chronological")
    relevant=[f for f in flows if start<stamp(f["stamp"])<=end]
    net=sum(float(f["amount_usd"]) for f in relevant)
    weighted=sum(float(f["amount_usd"])*(end-stamp(f["stamp"])).total_seconds()/seconds for f in relevant)
    base=previous["equity_usd"]+weighted
    return (current["equity_usd"]-previous["equity_usd"]-net)/base if base>0 else None

def path_report(observations,flows=()):
    if not observations: return {"status":"WAITING_FOR_BASELINE","curve":[]}
    rows=sorted(observations,key=lambda r:r["stamp"])
    base=rows[0]; usd_index=100.; gbp_index=100. if rows[0].get("gbpusd") else None; cash_usd=100.;cash_gbp=100.;peak=100.;drawdown=0.;curve=[]
    for i,row in enumerate(rows):
        if i:
            prev=rows[i-1];ret=dietz(prev,row,flows)
            usd_index=None if usd_index is None or ret is None else usd_index*(1+ret)
            # External flows are converted at the previous observed FX rate; this is an approximation.
            fx0,fx1=prev.get("gbpusd"),row.get("gbpusd")
            p=dict(prev,equity_usd=prev["equity_usd"]/fx0) if fx0 else None
            q=dict(row,equity_usd=row["equity_usd"]/fx1) if fx1 else None
            gflows=[dict(f,amount_usd=float(f["amount_usd"])/fx0) for f in flows] if fx0 else []
            gr=dietz(p,q,gflows) if p and q else None
            gbp_index=None if gbp_index is None or gr is None else gbp_index*(1+gr)
            dt=(stamp(row["stamp"])-stamp(prev["stamp"])).total_seconds()/86400
            for currency in ("USD","GBP"):
                rate=(prev.get("rates",{}).get(currency) or {}).get("annual_pct")
                value=cash_usd if currency=="USD" else cash_gbp
                value=None if value is None or rate is None else value*(1+rate/100*dt/(prev["rates"][currency].get("day_basis",365)))
                if currency=="USD":cash_usd=value
                else:cash_gbp=value
        if usd_index is not None:
            peak=max(peak,usd_index);drawdown=max(drawdown,(peak-usd_index)/peak*100)
        point={"stamp":row["stamp"],"Portfolio USD":usd_index,"Portfolio GBP":gbp_index,
               "USD cash proxy":cash_usd,"GBP cash proxy":cash_gbp}
        for name in ("SPY_USD","QQQ_USD","UK100_USD"):
            a=base.get("benchmarks",{}).get(name);b=row.get("benchmarks",{}).get(name)
            point[name]=100*b/a if a and b else None
            fx0,fx1=base.get("gbpusd"),row.get("gbpusd")
            point[name.replace("_USD","_GBP")]=100*(b/fx1)/(a/fx0) if a and b and fx0 and fx1 else None
        curve.append(point)
    # Completed UTC dates only. This is a since-inception record, not a reconstructed backtest.
    days={}
    for point in curve:
        if point["stamp"][:10]<rows[-1]["stamp"][:10]:days[point["stamp"][:10]]=point
    daily=list(days.values())
    returns=[];excess=[]
    for a,b in zip(daily,daily[1:]):
        if (stamp(b["stamp"]).date()-stamp(a["stamp"]).date()).days!=1:continue
        if not 23 <= (stamp(b["stamp"])-stamp(a["stamp"])).total_seconds()/3600 <= 25:continue
        if a["Portfolio USD"] and b["Portfolio USD"]:
            r=b["Portfolio USD"]/a["Portfolio USD"]-1;returns.append(r)
            if a["USD cash proxy"] and b["USD cash proxy"]:excess.append(r-(b["USD cash proxy"]/a["USD cash proxy"]-1))
    enough=len(returns)>=60
    sharpe=st.mean(excess)/st.stdev(excess)*math.sqrt(365) if enough and len(excess)==len(returns) and st.stdev(excess)>0 else None
    vol=st.stdev(returns)*math.sqrt(365)*100 if enough and st.stdev(returns)>0 else None
    elapsed=(stamp(rows[-1]["stamp"])-stamp(base["stamp"])).total_seconds()/86400
    return {"status":"BASELINE_RECORDED" if len(rows)==1 else "TRACKING",
            "baseline_at":base["stamp"],"latest_at":rows[-1]["stamp"],"observations":len(rows),
            "elapsed_days":elapsed,"initial_equity_usd":base["equity_usd"],"equity_usd":rows[-1]["equity_usd"],
            "cash_usd":rows[-1]["cash_usd"],"unrealized_pnl_usd":rows[-1]["unrealized_pnl_usd"],
            "return_usd_pct":usd_index-100 if usd_index is not None else None,
            "return_gbp_pct":gbp_index-100 if gbp_index is not None else None,
            "max_observed_drawdown_pct":drawdown,"complete_daily_returns":len(returns),"annual_volatility_pct":vol,
            "sharpe":sharpe,"cagr_pct":((usd_index/100)**(365/elapsed)-1)*100 if elapsed>=365 and usd_index and usd_index>0 else None,
            "flow_count":len(flows),"return_method":"Linked Modified Dietz intervals; assumes no external flows unless recorded in flows.json. Internal sales are not deposits.",
            "metrics_status":"Need60 complete daily returns for annual volatility/Sharpe and365 days for CAGR." if not enough else "Calendar-day annualization365; includes weekends.",
            "curve":curve}

def garch_grid(returns):
    if len(returns)<126:return {"status":"Need126 aligned returns"}
    r=returns[-252:];mean=st.mean(r);e=[x-mean for x in r];v0=max(variance(e),1e-12);best=None
    for alpha in (.03,.05,.10,.15):
        for beta in (.70,.80,.85,.90,.94):
            if alpha+beta>=.995:continue
            omega=v0*(1-alpha-beta);v=v0;loss=0.
            for z in e:
                v=max(v,1e-12);loss+=math.log(v)+z*z/v
                v=omega+alpha*z*z+beta*v
            if best is None or loss<best[0]:best=(loss,alpha,beta,omega,v)
    _,alpha,beta,omega,v=best
    return {"status":"DESCRIPTIVE_GRID_FIT","alpha":alpha,"beta":beta,"omega":omega,
            "next_day_vol_pct":100*math.sqrt(v),"annualized_conditional_vol_pct":100*math.sqrt(v*252),
            "normal_99pct_one_day_loss_pct":100*2.326347874*math.sqrt(v),"samples":len(r),
            "method":"GARCH(1,1), zero-mean residuals after window demeaning, variance targeting, finite fixed grid. Not a validated trading forecast or loss bound."}

def holding_diagnostics(holdings,data,fxdata,equity):
    prices={s:{b["fromDate"][:10]:float(b["close"]) for b in bars} for s,bars in data.items()}
    fx={b["fromDate"][:10]:float(b["close"]) for b in fxdata}
    panel=[];usable={};weights={};total=sum(max(0,h["value_usd"]) for h in holdings)
    for h in holdings:
        s=h["symbol"];p=prices.get(s,{})
        if s.endswith(".L"):p={d:x*fx[d] for d,x in p.items() if d in fx}
        dates=sorted(p);values=[p[d] for d in dates]
        item={**h,"bars":len(values),"last_candle":dates[-1] if dates else None,
              "return_21_sessions_pct":100*(values[-1]/values[-22]-1) if len(values)>=22 else None,
              "return_63_sessions_pct":100*(values[-1]/values[-64]-1) if len(values)>=64 else None,
              "return_252_sessions_pct":100*(values[-1]/values[-253]-1) if len(values)>=253 else None,
              "momentum_12_minus_1_pct":100*(values[-22]/values[-253]-1) if len(values)>=253 else None}
        panel.append(item)
        if h.get("history_eligible") and len(values)>=127:
            usable[s]=p
            weights[s]=weights.get(s,0)+max(0,h["value_usd"])/equity if equity>0 else 0
    covered=sum(weights.values())*equity
    risk={"status":"INSUFFICIENT_COVERAGE","holding_value_coverage_pct":100*covered/total if total else 100,
          "note":"Current holdings/current weights historical proxy; excludes costs, funding, dividends and unavailable/short/leveraged exposures. UK returns include daily GBPUSD changes. Not actual account history."}
    if not usable:return {"holdings":panel,"risk":risk}
    common=sorted(set.intersection(*(set(v) for v in usable.values())))[-253:]
    days=common[1:]
    if len(days)<126 or (total and covered/total<.95):
        return {"holdings":panel,"risk":risk}
    symbols=sorted(usable)
    series={s:[usable[s][b]/usable[s][a]-1 for a,b in zip(common,common[1:])] for s in symbols}
    sigma={(a,b):covariance(series[a],series[b])*(1 if a==b else .9) for a in symbols for b in symbols}
    variance_port=sum(weights[a]*weights[b]*sigma[a,b] for a in symbols for b in symbols)
    contributions=[{"symbol":a,"weight_pct":weights[a]*100,
        "variance_contribution_pct":100*weights[a]*sum(weights[b]*sigma[a,b] for b in symbols)/variance_port if variance_port>0 else None} for a in symbols]
    correlations=[]
    for i,a in enumerate(symbols):
        for b in symbols[i+1:]:
            denom=math.sqrt(variance(series[a])*variance(series[b]))
            if denom:correlations.append({"a":a,"b":b,"correlation":covariance(series[a],series[b])/denom})
    proxy=[sum(weights[s]*series[s][i] for s in symbols) for i in range(len(days))]
    risk.update(status="CURRENT_HOLDINGS_PROXY",samples=len(days),
                annual_volatility_pct=100*math.sqrt(max(0,variance_port)*252),
                variance_contributions=contributions,
                correlations=sorted(correlations,key=lambda r:abs(r["correlation"]),reverse=True)[:10],
                garch=garch_grid(proxy),shrinkage="10% off-diagonal shrinkage toward diagonal",
                calendar="252 common-date return observations, aligned before differencing. Local exchange closes differ; mixed crypto/equity weekends excluded.")
    return {"holdings":panel,"risk":risk}
