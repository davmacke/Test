import json
import math
import random
import unittest
from datetime import datetime,timedelta,timezone
import garch_model as g

def returns():
    rng=random.Random(39);v=.0001;out=[]
    for _ in range(400):
        e=rng.gauss(0,math.sqrt(v));out.append(e)
        v=.000005+.10*e*e+.85*v
    return out
class GarchTests(unittest.TestCase):
    def test_matches_photo_recursion_and_forecast_decay(self):
        m={"status":"FITTED_VARIANCE_TARGETED_QML","omega":.000005,"alpha":.1,"beta":.85,
           "persistence":.95,"next_variance":.0001,"long_run_variance":.0001}
        p=g.shock_projection(m,-4)
        self.assertAlmostEqual(p[0]["point_vol_pct"],math.sqrt(2.5))
        self.assertEqual(p,g.shock_projection(m,4))
        expected=.0001+.95**4*(.00025-.0001)
        self.assertAlmostEqual(p[1]["point_vol_pct"],100*math.sqrt(expected))
        self.assertGreater(p[0]["point_vol_pct"],p[-1]["point_vol_pct"])
        self.assertAlmostEqual(p[0]["cumulative_vol_pct"],p[0]["point_vol_pct"])
    def test_fit_is_positive_stationary_and_scale_invariant(self):
        r=returns();m=g.fit(r);scaled=g.fit([2*x for x in r])
        self.assertEqual(m["status"],"FITTED_VARIANCE_TARGETED_QML")
        self.assertGreater(m["omega"],0);self.assertLess(m["persistence"],.995)
        self.assertGreaterEqual(m["alpha"],0);self.assertGreaterEqual(m["beta"],0)
        self.assertAlmostEqual(scaled["next_day_vol_pct"],2*m["next_day_vol_pct"],places=5)
        self.assertAlmostEqual(m["long_run_variance"],m["omega"]/(1-m["persistence"]))
        json.dumps(m,allow_nan=False)
    def test_insufficient_constant_and_invalid_returns_fail_explicitly(self):
        self.assertEqual(g.fit([.01]*10)["status"],"INSUFFICIENT_HISTORY")
        self.assertEqual(g.fit([.01]*200)["status"],"DEGENERATE_VARIANCE")
        for bad in (float("nan"),float("inf"),1e200):
            self.assertEqual(g.fit([bad]*200)["status"],"INVALID_RETURNS")
        with self.assertRaises(ValueError):g.shock_projection({},float("nan"))
    def test_holdout_does_not_leak_into_training_parameters(self):
        r=returns();a=g.validate(r);changed=r[:-63]+[x*1.5 for x in r[-63:]];b=g.validate(changed)
        self.assertEqual(a["test_sessions"],63)
        self.assertEqual(a["training_parameters"],b["training_parameters"])
        self.assertNotEqual(a["average_qlike"],b["average_qlike"])
        self.assertEqual(set(a["average_qlike"]),{"GARCH","EWMA_094","Constant_variance"})
    def test_only_completed_candles_and_native_price_scale(self):
        now=datetime(2026,11,2,20,tzinfo=timezone.utc);end=now.replace(hour=0)
        series=[];price=100.
        for i,r in enumerate(returns()):
            price*=1+r
            series.append({"fromDate":(end-timedelta(days=400-i)).isoformat(),"open":price,"close":price,"high":price*1.01,"low":price*.99})
        unfinished={"fromDate":now.isoformat(),"open":price,"close":price/2,"high":price,"low":price/2}
        plain=g.instrument_forecasts({"SHEL.L":series},now)["forecasts"]["SHEL.L"]
        with_future=g.instrument_forecasts({"SHEL.L":series+[unfinished]},now)["forecasts"]["SHEL.L"]
        self.assertEqual(plain["next_variance"],with_future["next_variance"])
        pence=[{**b,**{k:b[k]*100 for k in ("open","high","low","close")}} for b in series]
        other=g.instrument_forecasts({"SHEL.L":pence},now)["forecasts"]["SHEL.L"]
        self.assertAlmostEqual(plain["next_day_vol_pct"],other["next_day_vol_pct"],places=7)
        self.assertEqual(g.instrument_forecasts({"SHEL.L":series},now+timedelta(days=10))["forecasts"]["SHEL.L"]["status"],"STALE_CANDLES")
if __name__=="__main__":unittest.main()
