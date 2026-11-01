import copy
import math
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import backtest as bt

DAY=datetime(2025,1,1,tzinfo=timezone.utc)
def stamp(i): return (DAY+timedelta(days=i)).isoformat()
def bar(i,price=100,low=99,high=101): return {"fromDate":stamp(i),"open":price,"high":high,"low":low,"close":price}
def signal(): return {"trend":{"action":"BUY CANDIDATE","stop_gap_pct":2}}

class BacktestTests(unittest.TestCase):
    def test_both_levels_hit_uses_stop_and_cash_reconciles(self):
        book=bt.Account("trend","base")
        book.step(stamp(0),{"TEST":bar(0,100,90,110)},{"TEST":signal()},{"TEST":"ETFs"})
        self.assertEqual(len(book.trades),1)
        self.assertEqual(book.trades[0]["reason"],"stop")
        self.assertLess(book.trades[0]["net_pnl_usd"],0)
        summary=book.finish(stamp(0))
        self.assertAlmostEqual(summary["net_pnl_usd"],sum(t["net_pnl_usd"] for t in book.trades),places=2)
        self.assertGreater(summary["modeled_costs_usd"],0)
    def test_gap_stop_uses_worse_open_and_no_same_day_reentry(self):
        book=bt.Account("trend","base")
        book.step(stamp(0),{"TEST":bar(0)},{"TEST":signal()},{"TEST":"ETFs"})
        self.assertEqual(len(book.positions),1)
        book.step(stamp(1),{"TEST":bar(1,90,89,91)},{"TEST":signal()},{"TEST":"ETFs"})
        self.assertEqual(book.trades[0]["reason"],"gap_stop")
        self.assertLess(book.trades[0]["exit"],90)
        self.assertFalse(book.positions)
    def test_stress_costs_and_financing_reduce_same_trade(self):
        results=[]
        for scenario in bt.SCENARIOS:
            book=bt.Account("trend",scenario)
            book.step(stamp(0),{"TEST":bar(0)},{"TEST":signal()},{"TEST":"Commodities"})
            book.step(stamp(3),{"TEST":bar(3)},{},{"TEST":"Commodities"})
            results.append(book.finish(stamp(3)))
            self.assertGreater(book.trades[0]["funding_usd"],0)
        self.assertLess(results[1]["final_usd"],results[0]["final_usd"])
    def test_future_changes_cannot_change_earlier_window(self):
        data={"SPY":[]}
        for i in range(170):
            price=100+i*.2+math.sin(i)*1.5
            data["SPY"].append(bar(i,price,price-.8,price+.8))
        first=bt.replay(data,{"SPY":"ETFs"})
        changed=copy.deepcopy(data)
        for row in changed["SPY"][-20:]:
            for key in ("open","high","low","close"): row[key]*=2
        second=bt.replay(changed,{"SPY":"ETFs"})
        self.assertEqual([r for r in first["summaries"] if r["window"]=="earlier_75pct"],
                         [r for r in second["summaries"] if r["window"]=="earlier_75pct"])
        self.assertEqual(len(first["summaries"]),16)
        self.assertTrue(all(not rows or all(r["equity"]>=0 for r in rows) for rows in first["curves"].values()))
    def test_signal_uses_prior_completed_candles_and_next_open(self):
        data={"TEST":[bar(i) for i in range(90)]}
        calls=[]
        def trend(past,current_price=None,**kwargs):
            calls.append((past[-1]["fromDate"],current_price))
            return {"action":"BUY CANDIDATE","stop_gap_pct":2}
        with patch.object(bt,"analyze_candles",side_effect=trend):
            report=bt.replay(data,{"TEST":"ETFs"})
        first=report["trades"]["earlier_75pct|trend|base"][0]
        self.assertEqual(calls[0][0],stamp(49))
        self.assertEqual(first["opened"],stamp(50))
        self.assertGreater(first["entry"],100)
        self.assertGreater(len(report["trades"]["earlier_75pct|trend|base"]),0)

if __name__=="__main__": unittest.main()
