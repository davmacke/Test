"""Variance-targeted GARCH(1,1), native simple returns; no trading side effects."""
import math
from statistics import mean
from datetime import timedelta
from bollinger_rsi import completed_daily_candles
from data_quality import age_seconds

VERSION="garch11_v2"
MIN_RETURNS=126
def _variance(values):
    center=mean(values)
    return sum((x-center)**2 for x in values)/len(values)

def _filter(residuals,variance,alpha,beta):
    omega=variance*(1-alpha-beta);v=variance;loss=0.;filtered=[]
    for e in residuals:
        filtered.append(v)
        loss+=math.log(v)+e*e/v
        v=max(omega+alpha*e*e+beta*v,1e-16)
    return loss,omega,v,filtered

def fit(returns):
    try:r=[float(x) for x in returns][-252:]
    except (ValueError,TypeError,OverflowError):return {"status":"INVALID_RETURNS"}
    if not all(math.isfinite(x) and abs(x)<=10 for x in r):return {"status":"INVALID_RETURNS"}
    if len(r)<MIN_RETURNS:return {"status":"INSUFFICIENT_HISTORY","samples":len(r),"required":MIN_RETURNS}
    center=mean(r);residuals=[x-center for x in r];variance=_variance(r)
    if variance<1e-14:return {"status":"DEGENERATE_VARIANCE","samples":len(r)}
    best=None
    def consider(alpha,beta):
        nonlocal best
        if alpha<0 or beta<0 or alpha+beta>=.995:return
        loss,omega,v,filtered=_filter(residuals,variance,alpha,beta)
        if best is None or loss<best[0]:best=(loss,alpha,beta,omega,v,filtered)
    for alpha in (0.,.01,.03,.05,.10,.15,.20,.30):
        for beta in (.0,.50,.70,.80,.85,.90,.94,.97):consider(alpha,beta)
    for step in (.02,.005,.001):
        for _ in range(4):
            _,a,b,*_=best
            for da in (-step,0,step):
                for db in (-step,0,step):consider(a+da,b+db)
    loss,alpha,beta,omega,v,filtered=best
    persistence=alpha+beta
    half_life=math.log(.5)/math.log(persistence) if 0<persistence<1 else 0.
    z2=[e*e/vt for e,vt in zip(residuals,filtered)]
    center_z=mean(z2);denom=sum((x-center_z)**2 for x in z2)
    lag1=sum((a-center_z)*(b-center_z) for a,b in zip(z2,z2[1:]))/denom if denom else None
    return {"status":"FITTED_VARIANCE_TARGETED_QML","version":VERSION,"samples":len(r),
            "mean_return":center,"omega":omega,"alpha":alpha,"beta":beta,"persistence":persistence,
            "next_variance":v,"long_run_variance":omega/(1-persistence),
            "next_day_vol_pct":100*math.sqrt(v),"long_run_vol_pct":100*math.sqrt(variance),
            "volatility_ratio":math.sqrt(v/variance),"half_life_sessions":half_life,
            "standardized_squared_residual_lag1":lag1,
            "boundary_fit":alpha<.001 or beta<.001 or persistence>.99,
            "fit_method":"Gaussian quasi-likelihood, variance targeting, bounded grid plus local refinement; not unconstrained full maximum likelihood."}

def project(model,horizons=(1,5,10,20),next_variance=None):
    if model.get("status")!="FITTED_VARIANCE_TARGETED_QML":return []
    v=model["next_variance"] if next_variance is None else next_variance
    p=model["persistence"];long=model["long_run_variance"]
    return [{"horizon_sessions":h,"point_vol_pct":100*math.sqrt(max(0,long+p**(h-1)*(v-long))),
             "cumulative_vol_pct":100*math.sqrt(max(0,sum(long+p**i*(v-long) for i in range(h))))}
            for h in horizons]

def shock_projection(model,shock_pct):
    shock=float(shock_pct)/100
    if not math.isfinite(shock) or abs(shock)>.5:raise ValueError("Shock must be finite and within 50%")
    if model.get("status")!="FITTED_VARIANCE_TARGETED_QML":return []
    # Shock denotes the innovation, not a predicted return. Both signs give the same GARCH response.
    following=model["omega"]+model["alpha"]*shock**2+model["beta"]*model["next_variance"]
    return project(model,next_variance=following)

def validate(returns):
    if not all(math.isfinite(x) and abs(x)<=10 for x in returns):return {"status":"INVALID_RETURNS","test_sessions":0}
    if len(returns)<MIN_RETURNS+63:return {"status":"NEED_189_RETURNS","test_sessions":0}
    values=list(returns)[-315:];training=values[:-63];testing=values[-63:]
    model=fit(training)
    if model["status"]!="FITTED_VARIANCE_TARGETED_QML":return {"status":model["status"],"test_sessions":0}
    center=model["mean_return"];constant=model["long_run_variance"]
    g=model["next_variance"];ew=constant
    for x in training:ew=.94*ew+.06*(x-center)**2
    losses={"GARCH":0.,"EWMA_094":0.,"Constant_variance":0.}
    for x in testing:
        e=x-center
        for name,var in (("GARCH",g),("EWMA_094",ew),("Constant_variance",constant)):
            losses[name]+=math.log(max(var,1e-16))+e*e/max(var,1e-16)
        g=model["omega"]+model["alpha"]*e*e+model["beta"]*g
        ew=.94*ew+.06*e*e
    losses={k:v/len(testing) for k,v in losses.items()}
    return {"status":"BETTER_RECENT_HOLDOUT" if losses["GARCH"]<min(losses["EWMA_094"],losses["Constant_variance"]) else "NO_CLEAR_ADVANTAGE",
            "test_sessions":len(testing),"training_sessions":len(training),"training_parameters":{k:model[k] for k in ("omega","alpha","beta","mean_return")},"average_qlike":losses,
            "method":"Last 63 observations withheld from parameter estimation; fixed trained parameters updated sequentially. Lower QLIKE is better. One historical split is not proof of future skill."}

def instrument_forecasts(data,now):
    forecasts={}
    for symbol,candles in data.items():
        bars=completed_daily_candles(candles,now)
        if not bars:
            forecasts[symbol]={"status":"NO_COMPLETED_CANDLES"};continue
        age=age_seconds(bars[-1]["fromDate"],now)
        if age is None or age>7*86400:
            forecasts[symbol]={"status":"STALE_CANDLES","as_of":bars[-1]["fromDate"]};continue
        returns=[b["close"]/a["close"]-1 for a,b in zip(bars,bars[1:])]
        result=fit(returns)
        result.update(as_of=bars[-1]["fromDate"],last_close=bars[-1]["close"],
                      return_basis="Native instrument quote currency; simple close-to-close returns. GBX/GBP scaling leaves volatility unchanged.",
                      forecast_origin="Next observation after the last completed candle; session horizons, not guaranteed calendar dates.")
        if result["status"]=="FITTED_VARIANCE_TARGETED_QML":
            result.update(projection=project(result),shock_4pct=shock_projection(result,-4),validation=validate(returns))
        forecasts[symbol]=result
    return {"version":VERSION,"evaluated_at":now.isoformat(),"forecasts":forecasts,
            "execution_mode":"DIAGNOSTIC_ONLY",
            "limitations":["GARCH estimates conditional volatility, not direction or geopolitical-event probabilities.",
              "Uses completed daily observations; today's incomplete move is not silently included as a completed return.",
              "Symmetric GARCH reacts equally to positive and negative innovations; leverage asymmetry and sudden structural breaks may be missed.",
              "Gaussian QML estimates variance; normal tail multipliers are not reliable loss bounds. Corporate-action adjustments are not independently validated.",
              "Per-instrument native-currency volatility is not aggregate account risk; portfolio risk needs covariance and adequate exposure coverage.",
              "Parameters are variance-targeted and locally refined; boundary fits and short holdouts limit confidence. Forecasts do not alter orders, stops or sizing."]}
