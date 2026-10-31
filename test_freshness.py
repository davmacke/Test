import ast
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    import requests
except ModuleNotFoundError:
    # Tests use a fake HTTP getter and must never call the network.
    sys.modules['requests'] = types.ModuleType('requests')
import data_quality as dq
import etoro_feed as ef
bot_tree = ast.parse(Path(__file__).with_name("demo_trader.py").read_text())
bot_tree.body = [n for n in bot_tree.body if not (isinstance(n, ast.ImportFrom) and n.module == "analysis")]
bot = types.ModuleType("bot_under_test")
bot.portfolio_risk = lambda *args: {"cash_usd": 1000}
exec(compile(bot_tree, "<demo-trader-test>", "exec"), bot.__dict__)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
META = {"instrument_id": 100063, "asset_class": "Crypto", "is_open": False}
FRESH = dict(META, is_open=True, is_buy_enabled=True, is_currently_tradable=True,
             status_checked_at=STAMP)
QUOTE = {"symbol": "SOL", "instrument_id": 100063, "bid": 99, "ask": 100,
         "last": 99.5, "quote_ts": STAMP, "fetched_at": STAMP}
BUY = {"action": "BUY CANDIDATE", "suggested_size_usd": 100, "stop_gap_pct": 3}
source = Path(__file__).with_name("app.py").read_text()
tree = ast.parse(source)
funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"connect", "load_mapping"}]
scope = {"sqlite3": sqlite3, "json": json, "DEFAULT_SYMBOLS": ["SOL"], "LIMIT": 1}
exec(compile(ast.Module(body=funcs, type_ignores=[]), "<app-functions>", "exec"), scope)

class Feed:
    def __init__(self, flags=None, error=False):
        self.flags = FRESH if flags is None else flags
        self.error = error
        self.refreshes = 0
        self.orders = []
    def resolve_symbols(self, symbols):
        self.refreshes += 1
        if self.error: raise RuntimeError("unavailable")
        return {"SOL": self.flags}
    def place_demo_order(self, order, request_id):
        self.orders.append(order)
        return {"orderId": 123}

class FreshnessTests(unittest.TestCase):
    def test_broker_time_is_independent_of_recent_fetch(self):
        self.assertEqual(dq.quote_problem(dict(QUOTE, quote_ts=(NOW-timedelta(hours=2)).isoformat()), NOW),
                         "Broker quote is stale")
        self.assertIsNone(dq.quote_problem(QUOTE, NOW))
    def test_unknown_future_and_stale_fetch_block(self):
        self.assertIn("unavailable", dq.quote_problem(dict(QUOTE, quote_ts=None), NOW))
        self.assertIn("future", dq.quote_problem(dict(QUOTE, quote_ts=(NOW+timedelta(minutes=1)).isoformat()), NOW))
        self.assertEqual(dq.quote_problem(dict(QUOTE, fetched_at=(NOW-timedelta(minutes=4)).isoformat()), NOW),
                         "Quote fetch is stale")
        self.assertIn("unavailable", dq.quote_problem(dict(QUOTE, quote_ts="2026-10-31T02:00:00"), NOW))
    def test_bad_prices_block(self):
        for values in ({"ask": float("nan")}, {"bid": 101}, {"bid": 0}):
            self.assertEqual(dq.quote_problem(dict(QUOTE, **values), NOW), "Invalid bid/ask quote")
    def test_cached_closed_flags_are_replaced(self):
        with patch.object(bot, "_now", return_value=NOW):
            reason, fresh = bot._evaluate_entry(Feed(), "SOL", META, QUOTE, BUY)
        self.assertIsNone(reason)
        self.assertTrue(fresh["is_open"])
    def test_status_failures_do_not_fall_back_to_cached_true(self):
        with patch.object(bot, "_now", return_value=NOW):
            for feed in (Feed(flags=dict(FRESH, is_open=None)), Feed(error=True),
                         Feed(flags=dict(FRESH, instrument_id=999))):
                reason, _ = bot._evaluate_entry(feed, "SOL", FRESH, QUOTE, BUY)
                self.assertIsNotNone(reason)
            reason, _ = bot._evaluate_entry(Feed(flags=dict(FRESH, is_open=False)), "SOL", FRESH, QUOTE, BUY)
            self.assertEqual(reason, "Market closed")
    def test_non_candidate_needs_no_status_request(self):
        feed = Feed()
        reason, _ = bot._evaluate_entry(feed, "SOL", META, QUOTE, {"action": "HOLD / WATCH"})
        self.assertEqual(feed.refreshes, 0)
        self.assertEqual(reason, "Strategy: HOLD / WATCH")
    def test_v1_date_last_execution_and_missing_timestamp(self):
        feed = ef.EtoroFeed.__new__(ef.EtoroFeed)
        feed.get = lambda *args: {"rates": [{"instrumentID": 100063, "date": STAMP,
                                          "bid": 99, "ask": 100, "lastExecution": 99.75}]}
        result = feed.fetch_rates({"SOL": META})[0]
        self.assertEqual(result["quote_ts"], STAMP)
        self.assertEqual(result["last"], 99.75)
        feed.get = lambda *args: {"rates": [{"instrumentID": 100063, "bid": 99, "ask": 100}]}
        result = feed.fetch_rates({"SOL": META})[0]
        self.assertIsNone(result["quote_ts"])
        self.assertIsNotNone(result["fetched_at"])
        self.assertEqual(result["last"], 99.5)
    def test_cache_reuses_identity_but_removes_dynamic_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"cache.json"
            path.write_text(json.dumps({"SOL": dict(FRESH), "_unresolved_symbols": []}))
            result = scope["load_mapping"](path, Feed(error=True))
            self.assertEqual(result["SOL"]["instrument_id"], 100063)
            self.assertNotIn("is_open", result["SOL"])
    def test_legacy_database_migration_and_order_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            scope["DB"] = Path(folder)/"test.db"
            c = sqlite3.connect(scope["DB"])
            c.execute("CREATE TABLE quotes(symbol TEXT,instrument_id INTEGER,ts TEXT,bid REAL,ask REAL,last REAL,change_pct REAL,asset_class TEXT,PRIMARY KEY(symbol,ts))")
            c.close()
            c = scope["connect"]()
            self.assertIn("quote_ts", {r["name"] for r in c.execute("PRAGMA table_info(quotes)")})
            c.execute("INSERT INTO market_analysis VALUES(?,?,?,?)", ("SOL", STAMP, "ok", json.dumps(BUY)))
            c.commit()
            feed = Feed()
            with patch.dict(os.environ, {"DEMO_AUTOTRADE_ENABLED": "true", "ETORO_TRADING_MODE": "demo", "DEMO_EXECUTION_CHECK_ID": ""}), patch.object(bot, "_now", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
                stale = dict(QUOTE, quote_ts=(NOW-timedelta(hours=1)).isoformat())
                snapshot = bot.run_demo_cycle(c, feed, {"SOL": META}, [stale],
                    {"clientPortfolio": {"credit": 1000, "positions": []}}, lambda *args: None)
                self.assertEqual(feed.orders, [])
                self.assertEqual(snapshot["entry_decisions"][0]["Decision"], "Broker quote is stale")
                snapshot = bot.run_demo_cycle(c, feed, {"SOL": META}, [QUOTE],
                    {"clientPortfolio": {"credit": 1000, "positions": []}}, lambda *args: None)
                self.assertEqual(len(feed.orders), 1)
                self.assertEqual(feed.orders[0]["amount"], 100)
                self.assertEqual(snapshot["entry_decisions"][0]["Decision"], "Demo order submitted")
            c.close()
            scope["connect"]().close()
    def test_all_changed_sources_parse(self):
        for filename in ("app.py", "demo_trader.py", "etoro_feed.py", "data_quality.py"):
            ast.parse(Path(__file__).with_name(filename).read_text(), filename=filename)

if __name__ == "__main__":
    unittest.main()
