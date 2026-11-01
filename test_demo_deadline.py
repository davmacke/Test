import contextlib
import io
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from test_bollinger_rsi import bot, scope, Feed, NOW, STAMP

class DeadlineTests(unittest.TestCase):
    def test_extension_preserves_run_and_stops_at_exact_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            scope["DB"]=Path(folder)/"test.db"
            c=scope["connect"]()
            old_end=(NOW+timedelta(hours=1)).isoformat()
            c.execute("""INSERT INTO demo_bot_state
                (id,started_at,ends_at,status,start_unrealized_pnl,last_unrealized_pnl,
                 start_realized_pnl,last_realized_pnl,stop_reason,run_key)
                VALUES(1,?,?,'RUNNING',7,7,9,9,'','trial')""",(STAMP,old_end))
            c.execute("""INSERT INTO demo_bot_orders
                (symbol,instrument_id,request_id,action,status,amount_usd,created_at,updated_at,run_key,pnl)
                VALUES('SOL',100063,'existing','open','OPEN',100,?,?,'trial',3)""",(STAMP,STAMP))
            c.commit()
            deadline="2026-11-02T21:00:00+00:00"
            config={"DEMO_AUTOTRADE_ENABLED":"true","ETORO_TRADING_MODE":"demo",
                "DEMO_RUN_ID":"trial","DEMO_EXECUTION_CHECK_ID":"","DEMO_RUN_ENDS_AT":deadline,
                "DEMO_MAX_OPEN_LOSS_USD":"0"}
            portfolio={"clientPortfolio":{"credit":1000,"positions":[],"closedPositionsNetProfit":9}}
            with patch.dict(os.environ,config),patch.object(bot,"_sync_orders"),patch.object(bot,"_close_order") as close,contextlib.redirect_stdout(io.StringIO()):
                with patch.object(bot,"_now",return_value=NOW+timedelta(hours=2)):
                    result=bot.run_demo_cycle(c,Feed(),{},[],portfolio,lambda *x:None)
                self.assertEqual(result["status"],"RUNNING")
                self.assertEqual(result["started_at"],STAMP)
                self.assertEqual(result["ends_at"],deadline)
                state=c.execute("SELECT * FROM demo_bot_state").fetchone()
                self.assertEqual((state["start_unrealized_pnl"],state["start_realized_pnl"]),(7,9))
                self.assertEqual(len(result["orders"]),1)
                close.assert_not_called()
                with patch.object(bot,"_now",return_value=bot.utc_timestamp(deadline)):
                    result=bot.run_demo_cycle(c,Feed(),{},[],portfolio,lambda *x:None)
                self.assertEqual(result["status"],"STOPPING")
                close.assert_called_once()
                c.execute("UPDATE demo_bot_orders SET status='CLOSED'")
                c.execute("UPDATE demo_bot_state SET status='STOPPED'")
                c.commit()
                with patch.object(bot,"_now",return_value=NOW),patch.dict(os.environ,{"DEMO_RUN_ENDS_AT":"2026-11-03T21:00:00Z"}):
                    result=bot.run_demo_cycle(c,Feed(),{},[],portfolio,lambda *x:None)
                self.assertEqual(result["status"],"STOPPED")
                self.assertEqual(result["ends_at"],deadline)
            c.close()

    def test_naive_deadline_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            scope["DB"]=Path(folder)/"test.db"
            c=scope["connect"]()
            config={"DEMO_AUTOTRADE_ENABLED":"true","ETORO_TRADING_MODE":"demo",
                "DEMO_EXECUTION_CHECK_ID":"","DEMO_RUN_ENDS_AT":"2026-11-02T21:00:00"}
            with patch.dict(os.environ,config),self.assertRaises(ValueError):
                bot.run_demo_cycle(c,Feed(),{},[],{"clientPortfolio":{"credit":1000}},lambda *x:None)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM demo_bot_state").fetchone()[0],0)
            c.close()

if __name__=="__main__": unittest.main()
