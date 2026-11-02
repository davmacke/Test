import ast
from datetime import datetime,timedelta,timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import test_freshness
import growth_runner as worker
from portfolio_evaluator import *

def observation(day,equity,fx=1.25):
    return {'stamp':day+'T23:00:00+00:00','equity_usd':equity,'cash_usd':100,
            'unrealized_pnl_usd':equity-1000,'gbpusd':fx,
            'rates':{'USD':{'annual_pct':3.6,'day_basis':360},'GBP':{'annual_pct':3.75,'day_basis':365}},
            'benchmarks':{'SPY_USD':equity/10,'QQQ_USD':equity/10,'UK100_USD':equity/10}}
def bars(returns):
    result=[];price=100;start=datetime(2025,1,1,tzinfo=timezone.utc)
    for i,r in enumerate([0]+returns):
        price*=1+r;result.append({'fromDate':(start+timedelta(days=i)).isoformat(),'close':price})
    return result
class GrowthTests(unittest.TestCase):
    def test_sale_is_not_a_deposit_and_external_flow_is_adjusted(self):
        a=observation('2026-11-02',1000);b=observation('2026-11-03',1500)
        self.assertEqual(dietz(a,b,[{'stamp':b['stamp'],'amount_usd':500}]),0)
        self.assertEqual(dietz(a,b,[]),.5)
        b['equity_usd']=1000
        self.assertEqual(dietz(a,b,[]),0)
    def test_short_record_no_annualization_and_currency_matching(self):
        r=path_report([observation('2026-11-02',1000),observation('2026-11-03',1100,1.375)])
        self.assertAlmostEqual(r['return_usd_pct'],10)
        self.assertAlmostEqual(r['return_gbp_pct'],0)
        self.assertIsNone(r['sharpe']);self.assertIsNone(r['cagr_pct'])
        self.assertAlmostEqual(r['curve'][-1]['SPY_GBP'],100)
        self.assertAlmostEqual(r['curve'][-1]['USD cash proxy'],100.01)
    def test_baseline_missing_fx_does_not_invent_gbp_return(self):
        a=observation('2026-11-02',1000,None)
        self.assertIsNone(path_report([a])['return_gbp_pct'])
    def test_completed_daily_history_and_no_gap_annualization(self):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        obs=[observation((start+timedelta(days=i)).date().isoformat(),1000*(1+.0002*i+(.0001 if i%2 else 0))) for i in range(65)]
        r=path_report(obs)
        self.assertGreaterEqual(r['complete_daily_returns'],60)
        self.assertIsNotNone(r['annual_volatility_pct'])
        self.assertIsNotNone(r['sharpe'])
        self.assertIsNone(r['cagr_pct'])
        self.assertEqual(path_report([obs[0],obs[3],obs[5]])['complete_daily_returns'],0)
    def test_rates_are_prior_known_and_missing_rate_not_zero_filled(self):
        a=observation('2026-11-02',1000);b=observation('2026-11-03',1000)
        b['rates']['USD']['annual_pct']=20
        r=path_report([a,b]);self.assertAlmostEqual(r['curve'][-1]['USD cash proxy'],100.01)
        a['rates']['USD']['annual_pct']=None
        self.assertIsNone(path_report([a,b])['curve'][-1]['USD cash proxy'])
    def test_covariance_risk_contributions_and_garch_constraints(self):
        x=[.01 if i%2 else -.008 for i in range(300)];y=[-.008 if i%2 else .01 for i in range(300)]
        holdings=[{'symbol':s,'value_usd':500,'history_eligible':True} for s in ('A','B')]
        r=holding_diagnostics(holdings,{'A':bars(x),'B':bars(y)},[],1000)['risk']
        self.assertEqual(r['status'],'CURRENT_HOLDINGS_PROXY')
        self.assertAlmostEqual(sum(c['variance_contribution_pct'] for c in r['variance_contributions']),100)
        self.assertLess(r['correlations'][0]['correlation'],-.99)
        g=r['garch'];self.assertLess(g['alpha']+g['beta'],1);self.assertGreater(g['omega'],0)
        self.assertTrue(math.isfinite(g['next_day_vol_pct']))
        self.assertEqual(garch_grid([.01]*10)['status'],'Need126 aligned returns')
    def test_missing_holdings_not_renormalized_and_fx_in_momentum(self):
        h=[{'symbol':'A','value_usd':500,'history_eligible':True},{'symbol':'MISSING','value_usd':500,'history_eligible':True}]
        self.assertEqual(holding_diagnostics(h,{'A':bars([.001]*300)},[],1000)['risk']['status'],'INSUFFICIENT_COVERAGE')
        a=bars([0]*300);fx=[dict(b,close=1+i/300) for i,b in enumerate(a)]
        h=[{'symbol':'RR.L','value_usd':1000,'history_eligible':True}]
        out=holding_diagnostics(h,{'RR.L':a},fx,1000)
        self.assertGreater(out['holdings'][0]['return_252_sessions_pct'],0)
        json.dumps(out,allow_nan=False)
    def test_rate_parser_and_seed_expiry(self):
        class Response:
            text='<h2>Current Bank Rate 3.75%</h2>Published on 17 September 2026'
            def raise_for_status(self):pass
            def json(self):return {'refRates':[{'type':'SOFR','effectiveDate':'2026-10-30','percentRate':3.94}]}
        now=datetime(2026,11,2,tzinfo=timezone.utc)
        with patch.object(worker.requests,'get',return_value=Response(),create=True):
            rates,errors=worker.rate_sources(now,{})
        self.assertEqual(rates['USD']['annual_pct'],3.94)
        self.assertEqual(rates['GBP']['annual_pct'],3.75)
        self.assertEqual(rates['USD']['status'],'SOURCE_FETCHED')
        with patch.object(worker.requests,'get',side_effect=OSError(),create=True):
            expired,_=worker.rate_sources(datetime(2026,11,12,tzinfo=timezone.utc),{})
        self.assertIsNone(expired['USD']['annual_pct']);self.assertIsNone(expired['GBP']['annual_pct'])
    def test_worker_has_no_order_api_calls_and_main_db_readonly(self):
        tree=ast.parse(Path(worker.__file__).read_text())
        self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr in {'place_demo_order','close_demo_position','cancel_demo_order'} for n in ast.walk(tree)))
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'main.db';c=sqlite3.connect(db)
            c.execute('CREATE TABLE demo_bot_orders(symbol TEXT,status TEXT,position_id INTEGER,created_at TEXT,entry_price REAL,strategy TEXT,amount_usd REAL)')
            c.execute('CREATE TABLE quotes(symbol TEXT,bid REAL,ask REAL,ts TEXT)')
            c.execute("INSERT INTO demo_bot_orders VALUES('A','OPEN',1,'2026-11-02T10:00:10+00:00',101,'trend',100)")
            c.execute("INSERT INTO quotes VALUES('A',99,101,'2026-11-02T10:00:00+00:00')");c.commit();c.close()
            orders,execution=worker.read_main(db)
            self.assertEqual(len(orders),1);self.assertAlmostEqual(execution[0]['mid_to_fill_bps'],100)
        json.dumps(path_report([observation('2026-11-02',1000)]),allow_nan=False)

    def test_full_evaluator_cycle_records_equity_and_strict_report(self):
        from contextlib import redirect_stdout
        import io
        now=datetime.now(timezone.utc)
        class Fake:
            def fetch_portfolio(self,mode):
                assert mode=='demo'
                return {'clientPortfolio':{'credit':100,'positions':[{'positionID':1,'instrumentID':2171,'amount':900,'isBuy':True,'leverage':1,'unrealizedPnL':{'pnL':9}}]}}
            def fetch_rates(self,mapping):
                return [{'symbol':s,'last':1.25 if s=='GBPUSD' else 100.,'quote_ts':now.isoformat()} for s in mapping]
            def fetch_candles(self,instrument,**kw):
                start=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=500)
                return [{'fromDate':(start+timedelta(days=i)).isoformat(),'open':100+i*.1,'close':100+i*.1,'high':101+i*.1,'low':99+i*.1} for i in range(500)]
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);folder=root/'growth';folder.mkdir()
            mapping={s:{'instrument_id':i,'asset_class':'Stocks'} for s,i in [('SHEL.L',2171),('SPY',1),('QQQ',2),('UK100',3),('GBPUSD',4)]}
            (root/'instrument_cache.json').write_text(json.dumps(mapping))
            main=root/'main.db';c=sqlite3.connect(main)
            c.execute('CREATE TABLE demo_bot_orders(symbol TEXT,status TEXT,position_id INTEGER,created_at TEXT,entry_price REAL,strategy TEXT,amount_usd REAL)')
            c.execute('CREATE TABLE quotes(symbol TEXT,bid REAL,ask REAL,ts TEXT)');c.commit();c.close()
            own=sqlite3.connect(folder/'growth.sqlite3')
            own.execute('CREATE TABLE observations(stamp TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            rates=observation('2026-11-02',1000)['rates']
            with patch.object(worker,'EtoroFeed',return_value=Fake()),patch.object(worker,'rate_sources',return_value=(rates,{})),patch.object(worker.time,'sleep'),redirect_stdout(io.StringIO()):
                worker.evaluate(main,folder,own)
            report=json.loads((folder/'report.json').read_text())
            self.assertEqual(report['equity_usd'],1009)
            self.assertEqual(report['observations'],1)
            self.assertEqual(report['risk']['status'],'CURRENT_HOLDINGS_PROXY')
            self.assertEqual(len(report['papers']),10)
            self.assertEqual(own.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
            with patch.object(worker,'EtoroFeed',return_value=Fake()),patch.object(worker,'rate_sources',return_value=(rates,{})),patch.object(worker,'read_main',side_effect=OSError()),patch.object(worker.time,'sleep'),redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):worker.evaluate(main,folder,own)
            self.assertEqual(own.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
            own.close()

if __name__=='__main__':unittest.main()
