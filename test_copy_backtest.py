import json
from pathlib import Path
import unittest
from copy_backtest import *

class CopyTests(unittest.TestCase):
    def row(self):
        return dict(id='x',filedAt='2024-01-05',transactedAt='2024-01-02',ticker='NVDA',assetType='stock',
                    side='buy',assetDescription='Purchased shares',source_url='test')
    def test_after_filing_not_transaction(self):
        row=self.row(); days=['2024-01-02','2024-01-05','2024-01-08','2024-01-09']
        ev,_=events({'pelosi':[row]},PELOSI,days)
        self.assertEqual(ev[0]['execute'],'2024-01-08')
        lag,_=events({'pelosi':[row]},PELOSI,days,1)
        self.assertEqual(lag[0]['execute'],'2024-01-09')
    def test_filters(self):
        for change in ({'assetType':'option'},{'assetDescription':'Exercised call options'},
                       {'assetDescription':'Contribution to Donor-Advised Fund'},
                       {'transactedAt':'2024-02-01'},{'needsReview':True}):
            r=self.row(); r.update(change); self.assertFalse(qualifying(r)[0])
        self.assertTrue(qualifying(self.row())[0])
    def test_future_disclosure_does_not_alter_past(self):
        a=self.row(); b=self.row(); b.update(id='y',filedAt='2024-03-01',transactedAt='2024-02-01',ticker='BE')
        days=['2024-01-08','2024-03-04']
        x,_=events({'pelosi':[a]},PELOSI,days); y,_=events({'pelosi':[a,b]},PELOSI,days)
        self.assertEqual(x[0],y[0]); self.assertEqual(x[0]['weights'],{'NVDA':.1})
    def test_berkshire_full_denominator(self):
        l={'berkshire':[dict(filed_at='2024-01-05',total_reported_value=1000,source_url='s',holdings=[dict(ticker='AAPL',cusip='1',value=500)])]}
        ev,_=events(l,BERKSHIRE,['2024-01-08'])
        self.assertAlmostEqual(ev[0]['weights']['AAPL'],.475)
    def test_missing_price_does_not_redistribute(self):
        a=Account(1000,1,'underlying_zero'); b={'A':{'open':10.,'close':10.}}
        a.step('2024-01-08',b,{'weights':{'A':.4,'MISSING':.4}})
        self.assertAlmostEqual(a.positions['A']['units'],40)
        self.assertGreater(a.cash,590); self.assertEqual(a.rebalances[0]['missing_weight_pct'],40)
    def test_fees_and_no_overspend(self):
        a=Account(1000,1,'underlying_2usd'); a.step('2024-01-08',{'A':{'open':10,'close':10}}, {'weights':{'A':1}})
        self.assertGreaterEqual(a.cash,-1e-8); self.assertEqual(a.orders[0]['commission_usd'],2)
        c=Account(1000,1,'cfd_base'); c.step('2024-01-08',{'A':{'open':10,'close':10}}, {'weights':{'A':.5}})
        self.assertAlmostEqual(c.orders[0]['commission_usd'],500*1.001*.0015)
    def test_closed_profit_only_payout(self):
        a=Account(1000,1,'underlying_zero'); a.step('2024-01-08',{'A':{'open':10,'close':12}},{'weights':{'A':.5}})
        a.month_end('2024-01-31'); self.assertEqual(a.withdrawn,0)
        a.step('2024-02-29',{'A':{'open':14,'close':14}},{'weights':{}})
        a.month_end('2024-02-29'); self.assertEqual(a.withdrawn,25)
    def test_ledger_and_strict_report(self):
        ledger=json.loads(Path(__file__).with_name('copy_disclosures.json').read_text(encoding='utf-8'))
        self.assertTrue(all(c['match'] for c in ledger['audit']))
        self.assertEqual(len(ledger['berkshire']),12)
        days=['2023-11-15','2024-01-02','2024-01-31','2024-07-03','2024-07-31','2024-12-31',
              '2025-01-02','2025-12-31','2026-01-02','2026-01-30','2026-08-24','2026-08-31','2026-09-01']
        symbols={'SPY'}|{h['ticker'] for f in ledger['berkshire'] for h in f['holdings'] if h['ticker']}
        symbols|={'NVDA','BE','INTC'}
        data={s:[dict(fromDate=d+'T00:00:00+00:00',open=100+i,close=100+i) for i,d in enumerate(days)] for s in symbols}
        data['GBPUSD']=[dict(fromDate='2026-10-30',close=1.33969)]
        report=replay_copy(data,ledger)
        self.assertEqual(len(report['summaries']),40)
        json.dumps(report,allow_nan=False)
if __name__=='__main__': unittest.main()
