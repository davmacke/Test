import copy
import json
import math
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import numpy as np
from research_strategies import *
from research_backtest import Book,costs,replay_research

START=datetime(2023,1,1,tzinfo=timezone.utc)
def date(i): return (START+timedelta(days=i)).isoformat()
def bars(n=600,phase=0):
    rows=[]
    for i in range(n):
        c=100+i*.05+math.sin(i*.7+phase)*2
        rows.append({"fromDate":date(i),"open":c-.1,"high":c+1,"low":c-1,"close":c})
    return rows

class ResearchTests(unittest.TestCase):
    def test_mean_reversion_filter_and_levels(self):
        f={"z":-2.2,"rsi":25,"close":100,"sma200":90,"atr":2,"mean20":110,"date":date(0)}
        b={"close":100,"sma200":90}
        sig=mean_reversion_signal(f,b)
        self.assertEqual((sig["stop"],sig["target"]),(96,110))
        self.assertIsNone(mean_reversion_signal(f,{"close":80,"sma200":90}))
        self.assertIsNone(mean_reversion_signal({**f,"z":-1},b))
    def test_mlp_is_train_scaled_deterministic_and_learns(self):
        rng=np.random.default_rng(4); x=rng.normal(size=(700,3))
        y=(x[:,0]+x[:,1]>.1).astype(float)
        a=SmallMLP().fit(x,y,epochs=250); b=SmallMLP().fit(x,y,epochs=250)
        self.assertGreater(np.mean((a.predict(x)>=.5)==y),.88)
        np.testing.assert_allclose(a.predict(x),b.predict(x))
        center=a.center.copy(); a.predict([[1e9,1e9,1e9]])
        np.testing.assert_array_equal(a.center,center)
    def test_walkforward_excludes_unmatured_labels_and_future_scaling(self):
        rng=np.random.default_rng(2)
        records=[{"x":rng.normal(size=8).tolist(),"y":float(i%2),
                  "label_end":date(i),"feature_date":date(i-5),"symbol":"SPY"} for i in range(700)]
        a,info=fit_walkforward(records,date(600))
        changed=copy.deepcopy(records)
        for r in changed[600:]: r["x"]=[1e12]*8; r["y"]=1
        b,_=fit_walkforward(changed,date(600))
        self.assertEqual(info["last_label_end"],date(599))
        np.testing.assert_allclose(a.center,b.center)
        np.testing.assert_allclose(a.w1,b.w1)
    def test_cointegration_real_test_and_bad_filter(self):
        rng=np.random.default_rng(42)
        x=5+np.cumsum(rng.normal(0,.01,180))
        e=np.zeros(180)
        for i in range(1,len(e)): e[i]=.7*e[i-1]+rng.normal(0,.001)
        y=.3+1.1*x+e
        history={"A":[{"fromDate":date(i),"close":float(np.exp(v))} for i,v in enumerate(y)],
                 "B":[{"fromDate":date(i),"close":float(np.exp(v))} for i,v in enumerate(x)]}
        model,info=calibrate_pair(history,"A","B")
        self.assertIsNotNone(model,info)
        self.assertLess(info["p_value"],.05/len(PAIRS))
        with patch("research_strategies.coint",return_value=(0,.9,[])):
            self.assertIsNone(calibrate_pair(history,"A","B")[0])
    def test_underlying_and_cfd_charges_are_not_combined(self):
        self.assertEqual(costs("underlying_base","SPY")[0],0)
        self.assertEqual(costs("cfd_base","SPY")[0],.0015)
        self.assertEqual(costs("cfd_stress","SPY")[0],.003)
        self.assertEqual(costs("underlying_base","SPY",True),costs("cfd_base","SPY",True))
    def test_single_stop_ordering_and_cash_reconciliation(self):
        b=Book(MEAN,"cfd_base",1250,1)
        signal={"strategy":MEAN,"stop":98,"target":104,"max_sessions":10,"signal_date":date(0)}
        current={"SPY":{"open":100,"high":110,"low":90,"close":100}}
        b.step(date(1),current,{"SPY":signal},{},{},set())
        self.assertEqual(len(b.trades),1)
        self.assertEqual(b.trades[0]["reason"],"stop")
        self.assertAlmostEqual(b.cash-1250,sum(t["net_pnl_usd"] for t in b.trades))
        self.assertLessEqual(b.trades[0]["gross_usd"],62.5)
        unfinished={**signal,"signal_date":date(1)}
        b.enter("SPY",unfinished,current,date(1))
        self.assertFalse(b.positions)
    def test_pair_reserves_both_legs_and_costs_both(self):
        b=Book(PAIRS_NAME,"underlying_base",1250,1)
        sig={"strategy":PAIRS_NAME,"a":"SPY","b":"VOO","alpha":0,"beta":1,
             "mu":0,"sd":.01,"z":2.2,"max_sessions":20,"signal_date":date(0)}
        current={s:{"open":100,"high":101,"low":99,"close":100} for s in ("SPY","VOO")}
        b.enter("SPY/VOO",sig,current,date(1))
        p=b.positions["SPY/VOO"]
        self.assertEqual({l["side"] for l in p["legs"]},{-1,1})
        self.assertLessEqual(p["gross"],125)
        self.assertAlmostEqual(1250-b.cash,p["gross"]+p["entry_fees"])
        self.assertAlmostEqual(p["entry_fees"],p["gross"]*.0015)
        b.close("SPY/VOO",{"SPY":90,"VOO":100},date(2),"test")
        self.assertAlmostEqual(b.cash-1250,b.trades[0]["net_pnl_usd"])
    def test_withdraw_only_profits_and_preserve_principal(self):
        b=Book(MEAN,"underlying_base",1250,1)
        b.cash=1300; b.trades.append({"net_pnl_usd":50}); b.month_end(date(30))
        self.assertEqual(b.withdrawals,25)
        self.assertEqual(b.cash,1275)
        self.assertAlmostEqual(b.months[-1]["return_pct"],4)
        b.cash=1200; b.month_end(date(60))
        self.assertEqual(b.withdrawals,25)
        b.cash=1240; b.trades.append({"net_pnl_usd":40}); b.month_end(date(90))
        self.assertEqual(b.withdrawals,25,"Recovery below initial capital cannot fund payout")
    def test_unrealized_gains_cannot_fund_withdrawal(self):
        b=Book(MEAN,"underlying_base",1250,1)
        b.cash=1300
        b.month_end(date(30))
        self.assertEqual(b.withdrawals,0)

    def test_whole_replay_future_invariance_and_monthly_accounting(self):
        data={"SPY":bars(),"QQQ":bars(phase=.7),"GBPUSD":[{**r,"close":1.34} for r in bars()]}
        first=replay_research(data)
        changed=copy.deepcopy(data)
        for s in ("SPY","QQQ"):
            for r in changed[s][-20:]:
                for field in ("open","high","low","close"): r[field]*=1.3
        second=replay_research(changed)
        json.dumps(first, allow_nan=False)
        self.assertEqual(len(first["summaries"]),18)
        before=[s for s in first["summaries"] if s["window"]=="earlier_75pct"]
        after=[s for s in second["summaries"] if s["window"]=="earlier_75pct"]
        self.assertEqual(before,after)
        self.assertTrue(any("last_label_end" in f for f in first["folds"]))
        for summary in first["summaries"]:
            key="|".join((summary["window"],summary["strategy"],summary["profile"]))
            pnl=sum(t["net_pnl_gbp"] for t in first["trades"][key])
            self.assertAlmostEqual(summary["economic_profit_gbp"],pnl,places=2)
            self.assertAlmostEqual(summary["final_gbp"]+summary["withdrawn_gbp"]-1250,
                                   summary["economic_profit_gbp"],places=1)
            self.assertTrue(all(m["withdrawal_gbp"]<=25+1e-8 for m in first["monthly"][key]))

if __name__=="__main__": unittest.main()
