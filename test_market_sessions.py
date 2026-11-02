"""Exchange-session and signal-generated demo-order tests. No network."""
import contextlib
import io
import json
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import market_sessions as ms
import test_freshness as h

def utc(value): return datetime.fromisoformat(value.replace('Z','+00:00'))

class SessionTests(unittest.TestCase):
    def test_london_open_while_us_preopen(self):
        now=utc('2026-11-02T10:00:00Z')
        self.assertTrue(ms.session('SHEL.L',now)['is_open'])
        self.assertFalse(ms.session('OXY',now)['is_open'])
        self.assertFalse(ms.session('OXY',utc('2026-11-02T14:29:59Z'))['is_open'])
        self.assertTrue(ms.session('OXY',utc('2026-11-02T14:30:00Z'))['is_open'])
        self.assertFalse(ms.session('OXY',utc('2026-11-02T21:00:00Z'))['is_open'])
        self.assertFalse(ms.session('SHEL.L',utc('2026-11-02T12:01:00Z'))['is_open'])
        self.assertTrue(ms.session('SHEL.L',utc('2026-11-02T12:03:00Z'))['is_open'])
    def test_holidays_weekends_early_closes_and_expiry(self):
        for s,d in [('SHEL.L','2026-12-25T10:00:00Z'),('SHEL.L','2026-12-28T10:00:00Z'),
                    ('SHEL.L','2027-03-29T10:00:00Z'),('OXY','2026-11-26T16:00:00Z'),
                    ('OXY','2027-06-18T16:00:00Z'),('OXY','2027-12-24T16:00:00Z'),
                    ('SHEL.L','2026-11-01T10:00:00Z'),('OXY','2028-01-03T16:00:00Z')]:
            self.assertFalse(ms.session(s,utc(d))['is_open'])
        self.assertTrue(ms.session('SHEL.L',utc('2026-12-24T12:29:59Z'))['is_open'])
        self.assertFalse(ms.session('SHEL.L',utc('2026-12-24T12:30:00Z'))['is_open'])
        self.assertTrue(ms.session('OXY',utc('2026-11-27T17:59:59Z'))['is_open'])
        self.assertFalse(ms.session('OXY',utc('2026-11-27T18:00:00Z'))['is_open'])
    def test_independent_dst_changes(self):
        self.assertEqual(ms.session('OXY',utc('2027-03-22T14:00:00Z'))['opens_at'],'2027-03-22T13:30:00+00:00')
        self.assertEqual(ms.session('SHEL.L',utc('2027-03-22T10:00:00Z'))['opens_at'],'2027-03-22T08:00:00+00:00')
        self.assertEqual(ms.session('SHEL.L',utc('2027-03-30T10:00:00Z'))['opens_at'],'2027-03-30T07:00:00+00:00')
        self.assertEqual(ms.session('OXY',utc('2027-11-01T15:00:00Z'))['opens_at'],'2027-11-01T13:30:00+00:00')
        self.assertEqual(ms.session('OXY',utc('2027-11-08T15:00:00Z'))['opens_at'],'2027-11-08T14:30:00+00:00')
    def test_only_unreliable_exchange_flag_gets_calendar_confirmation(self):
        now=utc('2026-11-02T10:00:00Z'); stamp=now.isoformat()
        meta=dict(h.FRESH,is_open=False,open_status_source='isExchangeOpen',status_checked_at=stamp)
        quote=dict(h.QUOTE,quote_ts=stamp,fetched_at=stamp)
        reason,fresh=ms.confirmed_market_status('SHEL.L',meta,quote,now)
        self.assertIsNone(reason);self.assertTrue(fresh['is_open']);self.assertFalse(meta['is_open'])
        self.assertEqual(fresh['open_status_source'],'exchange_calendar:XLON')
        for m,mode,symbol in [(dict(meta,open_status_source='isOpen'),'demo','SHEL.L'),
                              (dict(meta,is_buy_enabled=False),'demo','SHEL.L'),
                              (dict(meta,is_currently_tradable=False),'demo','SHEL.L'),
                              (dict(meta,is_currently_tradable=None),'demo','SHEL.L'),
                              (meta,'real','SHEL.L'),(meta,'demo','SOL'),(meta,'demo','EURGBP')]:
            self.assertIsNotNone(ms.confirmed_market_status(symbol,m,quote,now,mode)[0])
        old=dict(quote,quote_ts=(now-timedelta(seconds=31)).isoformat())
        self.assertIsNotNone(ms.confirmed_market_status('SHEL.L',meta,old,now)[0])
    def test_strategy_calculation_reaches_demo_broker_without_probe(self):
        now=utc('2026-11-02T10:00:00Z'); stamp=now.isoformat()
        with tempfile.TemporaryDirectory() as folder:
            h.scope['DB']=Path(folder)/'session.db'; c=h.scope['connect']()
            signal=dict(h.BUY,uk_reference_close=100)
            c.execute('INSERT INTO market_analysis VALUES(?,?,?,?)',('SHEL.L',stamp,'ok',json.dumps(signal)));c.commit()
            feed=h.Feed()
            meta=dict(h.FRESH,instrument_id=2171,is_open=False,open_status_source='isExchangeOpen',status_checked_at=stamp)
            feed.resolve_symbols=lambda symbols:{'SHEL.L':meta}
            quote=dict(h.QUOTE,symbol='SHEL.L',instrument_id=2171,quote_ts=stamp,fetched_at=stamp)
            cfg={'DEMO_AUTOTRADE_ENABLED':'true','ETORO_TRADING_MODE':'demo','DEMO_EXECUTION_CHECK_ID':'',
                 'DEMO_EZJ_EXECUTION_CHECK_ID':'','DEMO_RUN_ENDS_AT':''}
            with patch.dict(os.environ,cfg),patch.object(h.bot,'_now',return_value=now),patch.object(h.bot, "approve_entry", return_value={"version":"isolated-test","status":"APPROVED","reasons":["Approval isolated in broker/sizing test"],"evaluated_at":"2026-11-02T15:00:00+00:00"}), contextlib.redirect_stdout(io.StringIO()):
                snapshot=h.bot.run_demo_cycle(c,feed,{'SHEL.L':meta},[quote],{'clientPortfolio':{'credit':1000,'positions':[]}},lambda *args:None)
            self.assertEqual(len(feed.orders),1)
            self.assertEqual(feed.orders[0]['amount'],signal['suggested_size_usd'])
            self.assertAlmostEqual(feed.orders[0]['stopLossRate'],97)
            self.assertEqual(snapshot['entry_decisions'][0]['Decision'],'Demo order submitted')
            self.assertEqual(snapshot['orders'][0]['strategy'],'trend')
            self.assertNotEqual(snapshot['orders'][0]['strategy'],'execution_check')
            c.close()
if __name__=='__main__': unittest.main()
