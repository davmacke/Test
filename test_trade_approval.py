"""Approval decisions and real execution wiring; no HTTP calls."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import test_freshness as h
import trade_approval as a

NOW=datetime(2026,11,2,16,tzinfo=timezone.utc)
STAMP=NOW.isoformat()
def bars():
    end=datetime(2026,10,30,tzinfo=timezone.utc)
    return [{"fromDate":(end-timedelta(days=79-i)).isoformat(),
             "open":100+i*.2,"close":100+i*.2,"low":99+i*.2,"high":101+i*.2} for i in range(80)]
def metric():
    return a.measure({"approval_history":bars()},STAMP,NOW)[0]
def ctx():
    return {"version":a.VERSION,"metrics":{s:metric() for s in set(a.PRIORITY)|a.UK|a.CRYPTO|{"AAPL","OIL"}},
            "unavailable":{},"evaluated_at":STAMP}
def quote(symbol="AAPL"):
    price=bars()[-1]["close"]
    return {"symbol":symbol,"bid":price,"ask":price+.01,"last":price,"quote_ts":STAMP,"fetched_at":STAMP}
def review(symbol="AAPL",data=None,quotes=None):
    data=data or ctx()
    return a.approve(symbol,{},quote(symbol),data,quotes if quotes is not None else {s:quote(s) for s in data["metrics"]},NOW)

class ApprovalTests(unittest.TestCase):
    def test_positive_market_is_approved(self):
        self.assertEqual(review()["status"],"APPROVED")
    def test_benchmark_crash_vetoes_even_a_rising_stock(self):
        c=ctx();c["metrics"]["SPY"]["day_pct"]=-1.8
        r=review(data=c);self.assertEqual(r["status"],"VETO");self.assertIn("Benchmark sell-off",r["reasons"][0])
    def test_sector_selloff_vetoes(self):
        c=ctx();c["metrics"]["XLK"]["day_pct"]=-4
        self.assertIn("Sector",review(data=c)["reasons"][0])
    def test_broad_weakness_despite_flat_index(self):
        c=ctx()
        for s in a.SECTORS:c["metrics"][s]["day_pct"]=-.8
        r=review(data=c);self.assertEqual(r["status"],"VETO");self.assertIn("Broad weakness",r["reasons"][0])
    def test_falling_own_trend_and_lower_low_block(self):
        for field,changes in [("trend",{"close":90,"sma50_change":-1}),("candle",{"day_pct":-.4,"lower_low":True})]:
            with self.subTest(field=field):
                c=ctx();c["metrics"]["AAPL"].update(changes)
                self.assertEqual(review(data=c)["status"],"VETO")
    def test_missing_stale_and_misaligned_data_wait(self):
        c=ctx();del c["metrics"]["SPY"]
        self.assertEqual(review(data=c)["status"],"WAIT")
        c=ctx();c["metrics"]["XLK"]["date"]="2026-10-29"
        self.assertEqual(review(data=c)["status"],"WAIT")
        self.assertEqual(review(quotes={})["status"],"WAIT")
        c=ctx()
        for s in a.SECTORS[:3]:del c["metrics"][s]
        self.assertEqual(review(data=c)["status"],"WAIT")
    def test_intraday_crash_cannot_pass_positive_daily_snapshot(self):
        q={s:quote(s) for s in ctx()["metrics"]};q["SPY"]["bid"]*=.98;q["SPY"]["ask"]=q["SPY"]["bid"]+.01
        self.assertIn("intraday",review(quotes=q)["reasons"][0])
    def test_uk_crypto_and_commodities_are_not_us_equities(self):
        c=ctx();c["metrics"]["SPY"]["day_pct"]=-4
        for s in ("SHEL.L","SOL","OIL"):
            self.assertEqual(review(s,c)["status"],"APPROVED")
        c["metrics"]["UK100"]["day_pct"]=-2
        self.assertEqual(review("SHEL.L",c)["status"],"VETO")
        c["metrics"]["BTC"]["day_pct"]=-3
        self.assertEqual(review("SOL",c)["status"],"VETO")
        self.assertEqual(review("EEM",c)["status"],"WAIT")
    def test_measure_excludes_unfinished_candles_and_rejects_old_data(self):
        b=bars();future={**b[-1],"fromDate":NOW.isoformat(),"close":1,"low":1}
        m,error=a.measure({"approval_history":b+[future]},STAMP,NOW)
        self.assertIsNone(error);self.assertEqual(m["close"],b[-1]["close"])
        self.assertIsNone(a.measure({"approval_history":b},(NOW-timedelta(hours=3)).isoformat(),NOW)[0])
        self.assertIsNone(a.measure({"approval_history":b},(NOW+timedelta(seconds=1)).isoformat(),NOW)[0])
        self.assertIsNone(a.measure({"approval_history":b},STAMP,NOW+timedelta(days=10))[0])
    def test_real_demo_pipeline_audits_and_blocks_before_submission(self):
        for mode in ("approved","veto","missing"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                h.scope["DB"]=Path(folder)/"gate.db";c=h.scope["connect"]()
                try:
                    b=bars()
                    if mode=="veto":
                        b[-1].update(close=b[-2]["close"]*.97,open=b[-2]["close"]*.97,low=b[-2]["close"]*.96)
                    payload={**h.BUY,"approval_history":b if mode!="missing" else []}
                    c.execute("INSERT INTO market_analysis VALUES(?,?,?,?)",("OIL",STAMP,"ok",json.dumps(payload)));c.commit()
                    feed=h.Feed();meta=dict(h.FRESH,instrument_id=5,status_checked_at=STAMP)
                    feed.resolve_symbols=lambda symbols:{"OIL":meta}
                    config={"DEMO_AUTOTRADE_ENABLED":"true","ETORO_TRADING_MODE":"demo","DEMO_EXECUTION_CHECK_ID":"",
                            "DEMO_EZJ_EXECUTION_CHECK_ID":"","DEMO_USE_ALL_AVAILABLE_CASH":"false","DEMO_RUN_ENDS_AT":""}
                    with patch.dict(os.environ,config),patch.object(h.bot,"_now",return_value=NOW),contextlib.redirect_stdout(io.StringIO()):
                        result=h.bot.run_demo_cycle(c,feed,{"OIL":meta},[quote("OIL")],{"clientPortfolio":{"credit":1000,"positions":[]}},lambda *x:None)
                    expected={"approved":"APPROVED","veto":"VETO","missing":"WAIT"}[mode]
                    self.assertEqual(len(feed.orders),int(mode=="approved"))
                    self.assertEqual(result["entry_decisions"][0]["Approval"],expected)
                    self.assertEqual(c.execute("SELECT status FROM demo_approval_events").fetchone()[0],expected)
                finally:c.close()
if __name__=="__main__":unittest.main()
