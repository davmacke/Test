import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from sector_rotation import *
from test_bollinger_rsi import bot, scope, Feed

NOW=datetime(2026,11,1,12,tzinfo=timezone.utc)
STAMP=NOW.isoformat()

def bars(slope=.5, breakout=False):
    start=NOW.replace(hour=0)-timedelta(days=61)
    rows=[]
    for i in range(61):
        close=100+i*slope
        if breakout and i==60: close+=5
        rows.append({"fromDate":(start+timedelta(days=i)).isoformat(),
                     "open":close-.1,"high":close+1,"low":close-1,"close":close})
    return rows

def histories():
    result={"SPY":{"bars":bars(.1),"evaluated_at":STAMP}}
    result.update({s:{"bars":bars(.2+i*.05),"evaluated_at":STAMP} for i,s in enumerate(SECTORS)})
    result["OIL"]={"bars":bars(.3,True),"evaluated_at":STAMP}
    result["USO"]={"bars":bars(.3,True),"evaluated_at":STAMP}
    return result

class RotationTests(unittest.TestCase):
    def test_top_three_and_single_oil_preference(self):
        result=analyze_rotation(histories(),10000,NOW)
        buys=[s for s in SECTORS if result[s]["action"]=="BUY CANDIDATE"]
        self.assertEqual(set(buys),set(SECTORS[-3:]))
        self.assertEqual(result["OIL"]["action"],"BUY CANDIDATE")
        self.assertNotEqual(result["USO"]["action"],"BUY CANDIDATE")
        self.assertLess(result["OIL"]["stop_rate"],bars(.3,True)[-1]["close"])
        self.assertGreater(result["OIL"]["suggested_size_usd"],0)
    def test_missing_stale_and_misaligned_benchmark_block(self):
        for mode in ("missing","stale","misaligned"):
            h=histories()
            if mode=="missing": del h["SPY"]
            if mode=="stale": h["SPY"]["evaluated_at"]=(NOW-timedelta(hours=2)).isoformat()
            if mode=="misaligned":
                h["SPY"]["bars"][-1]["fromDate"]=(NOW.replace(hour=0)-timedelta(days=62)).isoformat()
            result=analyze_rotation(h,10000,NOW)
            self.assertTrue(all(result[s]["action"]=="INSUFFICIENT DATA" for s in SECTORS))
    def test_coverage_and_absolute_downtrend_block(self):
        h=histories()
        for s in SECTORS[7:]: del h[s]
        result=analyze_rotation(h,10000,NOW)
        self.assertTrue(all(result[s]["action"]!="BUY CANDIDATE" for s in SECTORS))
        h=histories()
        for s in SECTORS: h[s]["bars"]=bars(-.2)
        result=analyze_rotation(h,10000,NOW)
        self.assertTrue(all(result[s]["action"]=="REDUCE / AVOID" for s in SECTORS))
    def test_live_entry_bounds_and_staleness(self):
        signal=analyze_rotation(histories(),10000,NOW)["OIL"]
        price=bars(.3,True)[-1]["close"]
        plan,reason=plan_rotation_entry(signal,price,10000,NOW)
        self.assertIsNone(reason)
        self.assertLessEqual(plan["suggested_size_usd"],500)
        self.assertLessEqual(plan["suggested_size_usd"]*plan["stop_gap_pct"]/100,50)
        for p in (signal["stop_rate"],signal["target_rate"],float("nan")):
            self.assertIsNone(plan_rotation_entry(signal,p,10000,NOW)[0])
        self.assertIsNone(plan_rotation_entry(signal,price,10000,NOW+timedelta(hours=2))[0])
    def test_cache_integration_and_demo_order(self):
        h=histories()
        tree=ast.parse(Path(__file__).with_name("app.py").read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="refresh_analysis"]
        from bollinger_rsi import analyze_bollinger_rsi,completed_daily_candles
        namespace=dict(datetime=datetime,timezone=timezone,json=json,CANDLE_REFRESH_SECONDS=1800,
            APPROVAL_PRIORITY=(),CANDLES_PER_CYCLE=100,ROTATION_SYMBOLS=SUPPORTED,analyze_rotation=analyze_rotation,
            analyze_bollinger_rsi=analyze_bollinger_rsi,completed_daily_candles=completed_daily_candles,
            RISK_PER_TRADE_PCT=.5,MAX_POSITION_PCT=5, UK_SYMBOLS=(),
            analyze_candles=lambda *a,**kw:{"action":"HOLD / WATCH"},log=lambda *a:None)
        exec(compile(ast.Module(body=functions,type_ignores=[]),"<refresh-analysis>","exec"),namespace)
        mapping={s:{"instrument_id":i} for i,s in enumerate(h)}
        by_id={mapping[s]["instrument_id"]:v["bars"] for s,v in h.items()}
        class HistoryFeed:
            def fetch_candles(self,instrument_id,**kw): return by_id[instrument_id]
        class Clock:
            @classmethod
            def now(cls,tz): return NOW
            fromisoformat=datetime.fromisoformat
        with tempfile.TemporaryDirectory() as folder:
            scope["DB"]=Path(folder)/"test.db"
            c=scope["connect"]()
            namespace["datetime"]=Clock
            namespace["refresh_analysis"](c,HistoryFeed(),mapping,{},10000)
            payload=json.loads(c.execute("SELECT payload FROM market_analysis WHERE symbol='OIL'").fetchone()[0])
            self.assertEqual(payload["sector_rotation"]["action"],"BUY CANDIDATE")
            class TradeFeed(Feed):
                def resolve_symbols(self,symbols):
                    return {"OIL":{"instrument_id":mapping["OIL"]["instrument_id"],"is_open":True,
                        "is_buy_enabled":True,"is_currently_tradable":True,"status_checked_at":STAMP}}
            feed=TradeFeed()
            price=bars(.3,True)[-1]["close"]
            config={"DEMO_AUTOTRADE_ENABLED":"true","ETORO_TRADING_MODE":"demo","DEMO_RUN_ID":"rotation-test",
                "DEMO_RUN_HOURS":"8","DEMO_EXECUTION_CHECK_ID":"","DEMO_MAX_OPEN_LOSS_USD":"0"}
            quote={"symbol":"OIL","ask":price,"bid":price-.01,"quote_ts":STAMP,"fetched_at":STAMP}
            with patch.dict(os.environ,config),patch.object(bot,"_now",return_value=NOW),patch.object(bot, "approve_entry", return_value={"version":"isolated-test","status":"APPROVED","reasons":["Approval isolated in broker/sizing test"],"evaluated_at":"2026-11-02T15:00:00+00:00"}), contextlib.redirect_stdout(io.StringIO()):
                snapshot=bot.run_demo_cycle(c,feed,{"OIL":mapping["OIL"]},[quote],
                    {"clientPortfolio":{"credit":1000,"positions":[]}},lambda *x:None)
            self.assertEqual(len(feed.orders),1)
            self.assertEqual(snapshot["orders"][0]["strategy"],STRATEGY)
            self.assertEqual(feed.orders[0]["stopLossRate"],round(payload["sector_rotation"]["stop_rate"],8))
            self.assertEqual(feed.orders[0]["takeProfitRate"],round(payload["sector_rotation"]["target_rate"],8))
            c.close()

if __name__=="__main__": unittest.main()
