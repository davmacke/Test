"""Research-only signals derived from the three supplied educational diagrams."""
import math
import warnings
import numpy as np
from statsmodels.tsa.stattools import coint

ETF_UNIVERSE=("SPY","QQQ","DIA","IWM","RSP","XLK","XLF","XLE","XLI","XLV","XLY",
              "XLP","XLU","XLB","XLRE","XLC","SMH","SOXX","GLD","SLV","TLT","HYG","USO","EEM","EFA","VNQ")
PAIRS=(("SPY","VOO"),("SPY","VTI"),("QQQ","XLK"),("SMH","SOXX"),("XLE","XOP"),("GLD","GOLD"))
MEAN="filtered_mean_reversion_v1"
PAIRS_NAME="cointegrated_pairs_v1"
NEURAL="walkforward_mlp_v1"
MODES=(MEAN,PAIRS_NAME,NEURAL)

def features(rows):
    if len(rows)<60: return None
    recent=rows[-200:]
    c=np.asarray([r["close"] for r in recent],dtype=float)
    if not np.all(np.isfinite(c)) or np.any(c<=0): return None
    returns=np.diff(np.log(c[-21:]))
    sma20=float(np.mean(c[-20:])); sd=float(np.std(c[-20:]))
    if sd<1e-10: return None
    delta=np.diff(c[-15:])
    gain=float(np.mean(np.maximum(delta,0))); loss=float(np.mean(np.maximum(-delta,0)))
    rsi=50 if gain==loss==0 else (100 if loss==0 else 100-100/(1+gain/loss))
    tr=[max(recent[i]["high"]-recent[i]["low"],abs(recent[i]["high"]-recent[i-1]["close"]),
            abs(recent[i]["low"]-recent[i-1]["close"])) for i in range(len(recent)-14,len(recent))]
    atr=float(np.mean(tr)); last=float(c[-1]); z=(last-sma20)/sd
    x=[math.log(c[-1]/c[-2]),math.log(c[-1]/c[-6]),math.log(c[-1]/c[-21]),
       float(np.std(returns)),z,last/float(np.mean(c[-50:]))-1,rsi/100,atr/last]
    return {"x":x,"close":last,"z":float(z),"rsi":rsi,"atr":atr,"mean20":sma20,
            "sma50":float(np.mean(c[-50:])),"sma200":float(np.mean(c[-200:])) if len(c)>=200 else None,
            "date":recent[-1]["fromDate"]}

def mean_reversion_signal(f,benchmark):
    if not f or not benchmark or not f["sma200"] or not benchmark["sma200"]: return None
    if f["z"]<-2 and f["rsi"]<35 and f["close"]>f["sma200"] and benchmark["close"]>benchmark["sma200"]:
        return {"strategy":MEAN,"stop":f["close"]-2*f["atr"],"target":f["mean20"],
                "max_sessions":10,"signal_date":f["date"]}
    return None

def labeled_samples(data):
    records=[]
    for symbol in ETF_UNIVERSE:
        rows=data.get(symbol) or []
        for i in range(59,len(rows)-5):
            f=features(rows[max(0,i-199):i+1])
            if not f: continue
            # At t close, predict whether entry at t+1 open to t+5 close exceeds 20bp.
            y=float(rows[i+5]["close"]/rows[i+1]["open"]-1>.002)
            records.append({"symbol":symbol,"feature_date":rows[i]["fromDate"],
                "label_end":rows[i+5]["fromDate"],"x":f["x"],"y":y})
    return sorted(records,key=lambda r:(r["label_end"],r["symbol"],r["feature_date"]))

class SmallMLP:
    """One tanh hidden layer, train-only scaling, L2 and deterministic full-batch SGD."""
    def fit(self,x,y,epochs=120):
        x=np.asarray(x,dtype=float); y=np.asarray(y,dtype=float).reshape(-1,1)
        self.center=x.mean(axis=0); self.scale=x.std(axis=0)
        self.scale=np.where(self.scale<1e-8,1,self.scale)
        a=np.clip((x-self.center)/self.scale,-5,5)
        rng=np.random.default_rng(42)
        self.w1=rng.normal(0,1/math.sqrt(a.shape[1]),(a.shape[1],8))
        self.b1=np.zeros((1,8)); self.w2=rng.normal(0,.2,(8,1)); self.b2=np.zeros((1,1))
        for _ in range(epochs):
            h=np.tanh(a@self.w1+self.b1)
            p=1/(1+np.exp(-np.clip(h@self.w2+self.b2,-30,30)))
            d=(p-y)/len(a)
            gw2=h.T@d+.001*self.w2; gb2=d.sum(axis=0,keepdims=True)
            dh=(d@self.w2.T)*(1-h*h)
            gw1=a.T@dh+.001*self.w1; gb1=dh.sum(axis=0,keepdims=True)
            self.w1-=.05*gw1; self.b1-=.05*gb1
            self.w2-=.05*gw2; self.b2-=.05*gb2
        self.majority=float(np.mean(y)>=.5)
        return self
    def predict(self,x):
        x=np.asarray(x,dtype=float)
        if x.ndim==1: x=x.reshape(1,-1)
        a=np.clip((x-self.center)/self.scale,-5,5)
        h=np.tanh(a@self.w1+self.b1)
        return (1/(1+np.exp(-np.clip(h@self.w2+self.b2,-30,30)))).ravel()

def fit_walkforward(records,embargo_cutoff):
    selected=[r for r in records if r["label_end"]<embargo_cutoff][-4500:]
    if len(selected)<500 or len({r["y"] for r in selected})<2:
        return None,{"training_samples":len(selected),"reason":"Insufficient mature training labels"}
    model=SmallMLP().fit([r["x"] for r in selected],[r["y"] for r in selected])
    return model,{"training_samples":len(selected),"last_label_end":max(r["label_end"] for r in selected),
                  "embargo_cutoff":embargo_cutoff,"majority_class":model.majority}

def neural_signal(f,probability):
    if not f or probability is None: return None
    if probability>=.60 and f["close"]>f["sma50"]:
        distance=2*f["atr"]
        return {"strategy":NEURAL,"stop":f["close"]-distance,"target":f["close"]+2*distance,
                "max_sessions":5,"signal_date":f["date"],"probability":float(probability)}
    return None

def aligned(history,a,b,count=126):
    aa={r["fromDate"]:r for r in history.get(a,[])}
    bb={r["fromDate"]:r for r in history.get(b,[])}
    dates=sorted(set(aa)&set(bb))[-count:]
    return dates,np.array([aa[d]["close"] for d in dates]),np.array([bb[d]["close"] for d in dates])

def calibrate_pair(history,a,b):
    dates,aa,bb=aligned(history,a,b,126)
    if len(dates)<126: return None,{"reason":"Need 126 aligned prior sessions"}
    la,lb=np.log(aa),np.log(bb)
    beta,alpha=np.linalg.lstsq(np.column_stack([lb,np.ones(len(lb))]),la,rcond=None)[0]
    if not .25<=beta<=4: return None,{"reason":"Hedge ratio outside 0.25 to 4"}
    spread=la-alpha-beta*lb
    if np.std(spread)<1e-8: return None,{"reason":"Degenerate near-identical spread"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _,p,_=coint(la,lb,trend="c",maxlag=1,autolag=None)
    rho=np.linalg.lstsq(np.column_stack([spread[:-1],np.ones(len(spread)-1)]),spread[1:],rcond=None)[0][0]
    half=-math.log(2)/math.log(rho) if 0<rho<1 else math.inf
    info={"p_value":float(p) if math.isfinite(p) else None,"half_life_sessions":float(half) if math.isfinite(half) else None,
          "beta":float(beta),"last_training_date":dates[-1]}
    if not math.isfinite(p) or p>=.05/len(PAIRS) or not 1<=half<=30:
        return None,{**info,"reason":"Cointegration or half-life filter failed"}
    return {"a":a,"b":b,"alpha":float(alpha),"beta":float(beta)},info

def pair_signal(history,model):
    if not model: return None
    a,b=model["a"],model["b"]
    if not history.get(a) or not history.get(b): return None
    if history[a][-1]["fromDate"]!=history[b][-1]["fromDate"]: return None
    dates,aa,bb=aligned(history,a,b,60)
    if len(dates)<60: return None
    spread=np.log(aa)-model["alpha"]-model["beta"]*np.log(bb)
    mu,sd=float(np.mean(spread)),float(np.std(spread))
    if sd<1e-8: return None
    z=float((spread[-1]-mu)/sd)
    if not 2<=abs(z)<3: return None
    return {**model,"strategy":PAIRS_NAME,"mu":mu,"sd":sd,"z":z,
            "signal_date":dates[-1],"max_sessions":20}
