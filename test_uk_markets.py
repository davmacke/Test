"""UK universe and broker execution regressions; no network requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import etoro_feed as ef
import data_quality as dq
import test_freshness as harness
from analysis import analyze_candles

class UKMarketTests(unittest.TestCase):
    def test_uk_shares_fit_with_existing_candidates(self):
        selected=ef.DEFAULT_SYMBOLS[:120]
        self.assertEqual(len(selected),120)
        self.assertEqual(len(ef.UK_SYMBOLS),20)
        self.assertTrue(set(ef.UK_SYMBOLS).issubset(selected))
        self.assertTrue({'OXY','XLE','SPY','SOL','AAPL','HOOD'}.issubset(selected))
        self.assertEqual(len(set(selected)),len(selected))
        self.assertFalse(any(s.endswith('.L') for s in selected[:100]))
    def test_uk_exact_symbol_resolution(self):
        feed=ef.EtoroFeed.__new__(ef.EtoroFeed)
        feed.get=lambda *args: {'items':[{'instrumentId':1,'internalSymbolFull':'RR'},
                                          {'instrumentId':2,'internalSymbolFull':'RR.L','internalAssetClassName':'Stocks',
                                           'isExchangeOpen':True,'isCurrentlyTradable':True,'isBuyEnabled':True}]}
        meta=feed.resolve_symbols(['RR.L'])['RR.L']
        self.assertEqual(meta['instrument_id'],2)
        self.assertTrue(meta['is_open'])
    def test_native_price_scale_guard(self):
        self.assertIsNone(dq.uk_price_problem('RR.L',{'ask':1434},1430))
        self.assertIsNotNone(dq.uk_price_problem('RR.L',{'ask':14.34},1430))
        self.assertIsNotNone(dq.uk_price_problem('RR.L',{'ask':1434},14.30))
        self.assertIsNotNone(dq.uk_price_problem('RR.L',{'ask':1434},None))
        self.assertIsNone(dq.uk_price_problem('AAPL',{'ask':1434},None))
    def test_risk_budget_invariant_to_pounds_vs_pence(self):
        bars=[{'fromDate':f'2026-01-{i:03d}','close':100+i,'high':101+i,'low':99+i} for i in range(60)]
        pence=analyze_candles(bars,equity=1000)
        pounds=analyze_candles([{**b,**{k:b[k]/100 for k in ('close','high','low')}} for b in bars],equity=1000)
        self.assertEqual(pence['action'],pounds['action'])
        self.assertEqual(pence['suggested_size_usd'],pounds['suggested_size_usd'])
        self.assertAlmostEqual(pence['stop_gap_pct'],pounds['stop_gap_pct'])
    def test_demo_order_uses_usd_budget_and_native_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            harness.scope['DB']=Path(folder)/'uk.db'
            c=harness.scope['connect']()
            signal=dict(harness.BUY,uk_reference_close=1430)
            c.execute('INSERT INTO market_analysis VALUES(?,?,?,?)',('RR.L',harness.STAMP,'ok',json.dumps(signal)));c.commit()
            feed=harness.Feed()
            fresh=dict(harness.FRESH,instrument_id=2,asset_class='Stocks')
            feed.resolve_symbols=lambda symbols:{'RR.L':fresh}
            quote=dict(harness.QUOTE,symbol='RR.L',instrument_id=2,bid=1432,ask=1434,last=1433)
            with patch.dict(os.environ,{'DEMO_AUTOTRADE_ENABLED':'true','ETORO_TRADING_MODE':'demo','DEMO_EXECUTION_CHECK_ID':''}),patch.object(harness.bot,'_now',return_value=harness.NOW),contextlib.redirect_stdout(io.StringIO()):
                harness.bot.run_demo_cycle(c,feed,{'RR.L':fresh},[quote],{'clientPortfolio':{'credit':1000,'positions':[]}},lambda *args:None)
            self.assertEqual(len(feed.orders),1)
            order=feed.orders[0]
            self.assertEqual(order['orderCurrency'],'usd')
            self.assertEqual(order['amount'],100)
            self.assertAlmostEqual(order['stopLossRate'],1434*.97)
            self.assertNotIn('units',order)
            c.close()
if __name__=='__main__': unittest.main()
