import ast
import contextlib
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bollinger_rsi import *
import data_quality
bot_tree = ast.parse(Path(__file__).with_name("demo_trader.py").read_text())
bot_tree.body = [n for n in bot_tree.body if not (isinstance(n, ast.ImportFrom) and n.module == "analysis")]
bot = types.ModuleType("bot_bb_test")
bot.portfolio_risk = lambda *args: {"cash_usd": 1000, "estimated_account_value_usd": 10000}
exec(compile(bot_tree, "<bot-bb-test>", "exec"), bot.__dict__)

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
STAMP = NOW.isoformat()

def candles(wick=True):
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    result = []
    for i in range(40):
        close = 100-i
        result.append({"fromDate": (start+timedelta(days=i)).isoformat(),
                       "open": close+0.1, "high": close+1, "low": close-0.1, "close": close})
    if wick: result[-1]["low"] = 50
    return result

app_tree = ast.parse(Path(__file__).with_name("app.py").read_text())
scope = {"sqlite3": sqlite3}
exec(compile(ast.Module(body=[n for n in app_tree.body if isinstance(n, ast.FunctionDef)
                             and n.name == "connect"], type_ignores=[]), "<connect>", "exec"), scope)

class Feed:
    def __init__(self): self.orders, self.closes = [], []
    def resolve_symbols(self, symbols):
        return {"SOL": {"instrument_id": 100063, "is_open": True, "is_buy_enabled": True,
                        "is_currently_tradable": True, "status_checked_at": STAMP}}
    def place_demo_order(self, order, request_id):
        self.orders.append(order)
        return {"orderId": 123}
    def close_demo_position(self, *args):
        self.closes.append(args)
        return {"orderForClose": {"orderID": 124}}

class BollingerTests(unittest.TestCase):
    def test_population_bands_and_wilder_fixture(self):
        lower, middle, upper = bollinger_bands(list(range(1, 21)))
        self.assertEqual(middle, 10.5)
        self.assertAlmostEqual(upper-middle, 2*math.sqrt(33.25))
        self.assertAlmostEqual(middle-lower, upper-middle)
        fixture = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        self.assertAlmostEqual(wilder_rsi(fixture), 70.46413502109705)
        self.assertEqual(wilder_rsi([10]*20), 50)
    def test_wick_recovery_oversold_candidate(self):
        signal = analyze_bollinger_rsi(candles(), equity=10000, now=NOW)
        self.assertEqual(signal["action"], "BUY CANDIDATE")
        self.assertEqual(signal["rsi14"], 0)
        self.assertLess(signal["signal_low"], signal["lower_band"])
        self.assertLess(signal["lower_band"], signal["signal_close"])
        self.assertEqual(signal["stop_rate"], signal["lower_band"])
        self.assertEqual(signal["target_rate"], signal["middle_band"])
    def test_no_pierce_or_close_below_band_are_not_signals(self):
        self.assertEqual(analyze_bollinger_rsi(candles(False), now=NOW)["action"], "HOLD / WATCH")
        rows = candles()
        rows[-1].update(open=10, high=11, low=9, close=10)
        self.assertEqual(analyze_bollinger_rsi(rows, now=NOW)["action"], "HOLD / WATCH")
    def test_unfinished_and_invalid_bars_are_excluded(self):
        rows = candles(False)
        unfinished = {"fromDate": NOW.replace(hour=0).isoformat(), "open": 61, "high": 62, "low": 1, "close": 61}
        signal = analyze_bollinger_rsi(rows+[unfinished], now=NOW)
        self.assertEqual(signal["bars"], 40)
        self.assertEqual(signal["action"], "HOLD / WATCH")
        self.assertEqual(len(completed_daily_candles(rows+[rows[-1], {"close": 1}], NOW)), 40)
        self.assertEqual(analyze_bollinger_rsi(rows[:19], now=NOW)["action"], "INSUFFICIENT DATA")
    def test_rsi_filter_rejects_high_rsi(self):
        rows = candles()
        for i, row in enumerate(rows):
            close = 100+i
            row.update(open=close, high=close+1, low=close-1, close=close)
        rows[-1]["low"] = 1
        self.assertEqual(analyze_bollinger_rsi(rows, now=NOW)["action"], "HOLD / WATCH")
    def test_live_price_bounds_staleness_and_fixed_levels(self):
        signal = analyze_bollinger_rsi(candles(), equity=10000, now=NOW)
        for price in (signal["stop_rate"], signal["target_rate"], signal["stop_rate"]-1):
            self.assertIsNone(plan_bollinger_entry(signal, price, 10000, NOW)[0])
        self.assertIsNone(plan_bollinger_entry(signal, 62, 10000, NOW+timedelta(hours=2))[0])
        plan, reason = plan_bollinger_entry(signal, 62, 10000, NOW)
        self.assertIsNone(reason)
        self.assertEqual(plan["stop_rate"], signal["lower_band"])
        self.assertEqual(plan["target_rate"], signal["middle_band"])
        self.assertLessEqual(plan["suggested_size_usd"], 500)
        self.assertLessEqual(plan["suggested_size_usd"] * plan["stop_gap_pct"]/100, 50)
    def test_selection_priority_and_trend_fallback(self):
        bb = analyze_bollinger_rsi(candles(), equity=10000, now=NOW)
        strategy, selected = bot._select_strategy({"action": "BUY CANDIDATE", "bollinger_rsi": bb})
        self.assertEqual(strategy, STRATEGY)
        self.assertIs(selected, bb)
        strategy, selected = bot._select_strategy({"action": "BUY CANDIDATE", "bollinger_rsi": {"action": "HOLD / WATCH"}})
        self.assertEqual(strategy, "trend")
        self.assertEqual(selected["action"], "BUY CANDIDATE")
    def test_new_run_executes_bb_and_ignores_trend_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            scope["DB"] = Path(folder)/"test.db"
            c = scope["connect"]()
            # Preserve an old completed run and its historical order.
            c.execute("""INSERT INTO demo_bot_state
                      (id,started_at,ends_at,status,start_unrealized_pnl,last_unrealized_pnl,
                       start_realized_pnl,last_realized_pnl,stop_reason,run_key)
                       VALUES(1,?,?,'STOPPED',0,0,0,0,'ended','legacy')""", (STAMP, STAMP))
            c.execute("""INSERT INTO demo_bot_orders
                      (symbol,instrument_id,request_id,action,status,amount_usd,created_at,updated_at)
                       VALUES('SOL',100063,'old','open','CLOSED',10,?,?)""", (STAMP, STAMP))
            signal = analyze_bollinger_rsi(candles(), equity=10000, now=NOW)
            payload = {"action": "REDUCE / AVOID", "bollinger_rsi": signal}
            c.execute("INSERT INTO market_analysis VALUES(?,?,?,?)", ("SOL", STAMP, "ok", json.dumps(payload)))
            c.commit()
            quote = {"symbol": "SOL", "ask": 62, "bid": 61.99, "quote_ts": STAMP, "fetched_at": STAMP}
            feed = Feed()
            portfolio = {"clientPortfolio": {"credit": 1000, "positions": []}}
            config = {"DEMO_AUTOTRADE_ENABLED": "true", "ETORO_TRADING_MODE": "demo",
                      "DEMO_EXECUTION_CHECK_ID": "", "DEMO_RUN_ID": "bb-test", "DEMO_RUN_HOURS": "8"}
            with patch.dict(os.environ, config), patch.object(bot, "_now", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
                snapshot = bot.run_demo_cycle(c, feed, {"SOL": {"instrument_id": 100063}}, [quote], portfolio, lambda *x: None)
                self.assertEqual(snapshot["status"], "RUNNING")
                self.assertEqual(snapshot["run_key"], "bb-test")
                self.assertEqual(len(feed.orders), 1)
                order = feed.orders[0]
                self.assertAlmostEqual(order["stopLossRate"], signal["lower_band"], places=7)
                self.assertAlmostEqual(order["takeProfitRate"], signal["middle_band"], places=7)
                self.assertEqual(order["leverage"], 1)
                current = c.execute("SELECT * FROM demo_bot_orders WHERE run_key='bb-test'").fetchone()
                self.assertEqual(current["strategy"], STRATEGY)
                self.assertEqual(current["signal_candle"], signal["signal_candle"])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM demo_bot_orders").fetchone()[0], 2)
                c.execute("UPDATE demo_bot_orders SET status='OPEN',position_id=42 WHERE run_key='bb-test'")
                c.commit()
                portfolio["clientPortfolio"]["positions"] = [{"positionID": 42, "instrumentID": 100063}]
                again = bot.run_demo_cycle(c, feed, {"SOL": {"instrument_id": 100063}}, [quote], portfolio, lambda *x: None)
                self.assertEqual(again["started_at"], snapshot["started_at"])
                self.assertEqual(len(feed.orders), 1)
                self.assertEqual(feed.closes, [], "Trend exit must not close a Bollinger trade")
                # New run requests cannot abandon an active existing order.
                with patch.dict(os.environ, {"DEMO_RUN_ID": "next-run"}):
                    blocked = bot.run_demo_cycle(c, feed, {}, [], portfolio, lambda *x: None)
                self.assertEqual(blocked["status"], "BLOCKED")
                c.execute("UPDATE demo_bot_orders SET status='CLOSED' WHERE run_key='bb-test'")
                c.execute("UPDATE demo_bot_state SET status='STOPPED' WHERE id=1")
                c.commit()
                stopped = bot.run_demo_cycle(c, feed, {}, [], portfolio, lambda *x: None)
                self.assertEqual(stopped["status"], "STOPPED", "Same run ID cannot restart an ended run")
            c.close()

if __name__ == "__main__":
    unittest.main()
