import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import broker_accounting as a
import test_freshness as h
from analysis import portfolio_risk
from datetime import datetime,timezone

class AccountingTests(unittest.TestCase):
    def test_nested_position_pnl_and_pending_reservations(self):
        p={'clientPortfolio':{'credit':1000,'positions':[{'amount':400,'unrealizedPnL':{'pnL':25}}],
             'ordersForOpen':[{'orderID':5,'mirrorID':0,'amount':100,'totalExternalCosts':2}],
             'orders':[{'orderID':6,'amount':50}]}}
        t=a.totals(p)
        self.assertEqual(t['cash_usd'],850)
        self.assertEqual(t['equity_usd'],1427)
        self.assertEqual(t['unrealized_pnl_usd'],25)
        self.assertEqual(portfolio_risk(p)['estimated_account_value_usd'],1427)
    def test_copy_cash_and_realized_pnl_not_double_counted(self):
        p={'clientPortfolio':{'credit':100,'positions':[],'mirrors':[
            {'availableAmount':50,'closedPositionsNetProfit':10,'positions':[{'amount':200,'unrealizedPnL':{'pnL':-5}}]}]}}
        self.assertEqual(a.totals(p)['equity_usd'],345)
    def test_cash_not_equity_is_spendable(self):
        p={'clientPortfolio':{'credit':0,'positions':[{'amount':10000,'unrealizedPnL':{'pnL':1000}}]}}
        self.assertEqual(a.totals(p)['cash_usd'],0)
        self.assertEqual(a.totals(p)['equity_usd'],11000)
    def test_unknown_local_order_reserved_but_broker_pending_not_twice(self):
        rows=[{'action':'open','status':'PENDING','amount_usd':100,'order_id':5,'position_id':None},
              {'action':'open','status':'UNKNOWN','amount_usd':200,'order_id':None,'request_id':'x','position_id':None},
              {'action':'open','status':'OPEN','amount_usd':300,'position_id':7}]
        p={'clientPortfolio':{'credit':1000,'ordersForOpen':[{'orderID':5,'mirrorID':0,'amount':100}],
                              'positions':[{'positionID':7,'amount':300}]}}
        self.assertEqual(a.unreflected_reserve(rows,p),200)
        self.assertEqual(a.totals(p)['cash_usd']-a.unreflected_reserve(rows,p),700)
    def test_nonfinite_cash_fails_closed(self):
        for credit in (None,float('nan'),float('inf')):
            with self.assertRaises(ValueError): a.totals({'clientPortfolio':{'credit':credit}})
    def test_new_sale_cash_automatically_expands_budget_without_new_run(self):
        now=datetime(2026,11,2,15,0,tzinfo=timezone.utc);stamp=now.isoformat()
        initial={'clientPortfolio':{'credit':1000,'positions':[]}}
        fresh={'clientPortfolio':{'credit':5000,'positions':[{'positionID':1,'instrumentID':999,'amount':400,'unrealizedPnL':{'pnL':10}}]}}
        with tempfile.TemporaryDirectory() as folder:
            h.scope['DB']=Path(folder)/'funds.db';c=h.scope['connect']()
            cfg={'DEMO_AUTOTRADE_ENABLED':'true','ETORO_TRADING_MODE':'demo','DEMO_EXECUTION_CHECK_ID':'',
                 'DEMO_EZJ_EXECUTION_CHECK_ID':'','DEMO_RUN_ENDS_AT':'','DEMO_USE_ALL_AVAILABLE_CASH':'false'}
            feed=h.Feed()
            with patch.dict(os.environ,cfg),patch.object(h.bot,'_now',return_value=now),contextlib.redirect_stdout(io.StringIO()):
                h.bot.run_demo_cycle(c,feed,{},[],initial,lambda *args:None)
            c.execute('UPDATE demo_bot_state SET cash_budget_usd=400 WHERE id=1')
            key=c.execute('SELECT run_key FROM demo_bot_state').fetchone()[0]
            c.execute("""INSERT INTO demo_bot_orders(symbol,instrument_id,request_id,action,status,amount_usd,
                         created_at,updated_at,position_id,run_key) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         ('OLD',999,'old','open','OPEN',400,stamp,stamp,1,key))
            c.execute('INSERT INTO market_analysis VALUES(?,?,?,?)',('SHEL.L',stamp,'ok',json.dumps(dict(h.BUY,uk_reference_close=100))));c.commit()
            baseline=dict(c.execute('SELECT * FROM demo_bot_state').fetchone())
            meta=dict(h.FRESH,instrument_id=2171,status_checked_at=stamp)
            feed.resolve_symbols=lambda symbols:{'SHEL.L':meta}
            reads=[]
            def fetch(mode): reads.append(mode);return fresh
            feed.fetch_portfolio=fetch
            quote=dict(h.QUOTE,symbol='SHEL.L',instrument_id=2171,quote_ts=stamp,fetched_at=stamp)
            with patch.dict(os.environ,{**cfg,'DEMO_USE_ALL_AVAILABLE_CASH':'true'}),patch.object(h.bot,'_now',return_value=now),patch.object(h.bot,'portfolio_risk',side_effect=portfolio_risk),contextlib.redirect_stdout(io.StringIO()):
                result=h.bot.run_demo_cycle(c,feed,{'SHEL.L':meta},[quote],initial,lambda *args:None)
                self.assertEqual(result['cash_budget_usd'],5400)
                self.assertEqual(len(feed.orders),1)
                fresh['clientPortfolio']['credit']=7000
                result=h.bot.run_demo_cycle(c,feed,{'SHEL.L':meta},[quote],initial,lambda *args:None)
            self.assertEqual(result['cash_budget_usd'],7400)
            self.assertEqual(len(feed.orders),1)
            self.assertEqual(reads,['demo','demo'])
            current=dict(c.execute('SELECT * FROM demo_bot_state').fetchone())
            for k in ('run_key','started_at','ends_at','start_unrealized_pnl','start_realized_pnl'):
                self.assertEqual(current[k],baseline[k])
            c.close()
if __name__=='__main__':unittest.main()
