import unittest
from monitoring import performance,alerts

class MonitoringTests(unittest.TestCase):
    def test_trade_metrics_do_not_invent_net_returns(self):
        r=performance([{"run_key":"x","strategy":"trend","status":"CLOSED","pnl":12},
                       {"run_key":"x","strategy":"trend","status":"CLOSED","pnl":-5},
                       {"run_key":"x","strategy":"trend","status":"OPEN","pnl":3}], "x")
        self.assertEqual(r["realized_usd"],7)
        self.assertEqual(r["win_rate_pct"],50)
        self.assertIsNone(r["net_after_costs_usd"])
        self.assertIsNone(r["max_drawdown_pct"])
    def test_headline_does_not_execute(self):
        result=alerts([{"title":"Nvidia CEO resignation reportedly under investigation","domain":"example.com"}],["NVDA"])
        self.assertEqual(result[0]["verification"],"UNVERIFIED_HEADLINE")
        self.assertEqual(result[0]["action"],"MANUAL_REVIEW_ONLY")
if __name__=="__main__":unittest.main()
