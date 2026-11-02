import base64
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from finance import analyse,settings
from collector import parse_listing,canonical,public_url,Fetcher,discover,scan
from store import Store
from app import Application,handler

NOW=datetime(2026,11,2,12,tzinfo=timezone.utc)
def house(**kw):
 p=dict(url='https://example.com/property/1',price=70000,rent=650,beds=2,
        country='England',route='ready',tenure='freehold',annual_charges=0,
        listed='unknown',sale_status='advertised',observed_at=NOW.isoformat(),works=2000,evidence={})
 return p|kw

class FinanceTests(unittest.TestCase):
 def test_cashflow_and_capital(self):
  a=analyse(house(),now=NOW);f=a['financial']
  self.assertAlmostEqual(f['cashflow'],224.65);self.assertEqual(f['initial'],19700)
  self.assertAlmostEqual(f['stress_cashflow'],137.15);self.assertEqual(a['status'],'model_fits')
 def test_refurb_holding_costs(self):
  f=analyse(house(route='refurb',works=5500,vacant_months=3),now=NOW)['financial']
  self.assertAlmostEqual(f['initial'],24656.25);self.assertAlmostEqual(f['headroom'],343.75)
 def test_100k_not_with_works(self):self.assertEqual(analyse(house(price=100000),now=NOW)['status'],'excluded')
 def test_70k_7000_works_over_budget(self):self.assertEqual(analyse(house(route='refurb',works=7000,vacant_months=3),now=NOW)['status'],'excluded')
 def test_missing_rent_is_not_zero(self):self.assertIsNone(analyse(house(rent=None),now=NOW)['financial']['cashflow'])
 def test_missing_charges_is_not_zero(self):self.assertIsNone(analyse(house(annual_charges=None),now=NOW)['financial']['cashflow'])
 def test_grade_ii_always_excluded(self):self.assertEqual(analyse(house(listed='listed'),now=NOW)['status'],'excluded')
 def test_sold_and_wrong_beds(self):
  for kw in [dict(sale_status='sstc'),dict(beds=1),dict(country='Wales'),dict(cash_only=True)]:self.assertEqual(analyse(house(**kw),now=NOW)['status'],'excluded')
 def test_verified_needs_all_evidence(self):
  p=house(listed='unlisted',mortgage='confirmed',sale_status='available',evidence={k:'Confirmed 2 Nov with supporting document' for k in ['rent','costs','listed','mortgage','works','title']})
  self.assertEqual(analyse(p,now=NOW)['status'],'verified')
  self.assertNotEqual(analyse(p|dict(observed_at=(NOW-timedelta(days=8)).isoformat()),now=NOW)['status'],'verified')
 def test_invalid_settings(self):
  for v in [dict(ltv=1),dict(rate=float('nan')),dict(management=1),dict(budget=-1)]:
   with self.assertRaises(ValueError):settings(v)

class ParserTests(unittest.TestCase):
 def parse(self,body):return parse_listing(body,'https://example.com/property/1',dict(name='Test agent',country='England'))
 def test_current_rent_not_gross_yield(self):
  p=self.parse('<h1>2 bedroom house for sale</h1><p>£70,000 Freehold. Currently let at £650 pcm.</p>')
  self.assertEqual(p['price'],70000);self.assertEqual(p['rent'],650);self.assertEqual(p['listed'],'unknown')
 def test_annual_rent(self):
  p=self.parse('<h1>2 bed house for sale £70,000</h1><p>Annual gross income of £7,800</p>')
  self.assertEqual(p['rent'],650)
 def test_refurb_and_cash_only(self):
  p=self.parse('<h1>3 bed house for sale £70,000</h1><p>In need of modernisation. Cash buyers only. Grade II listed building.</p>')
  self.assertEqual(p['route'],'refurb');self.assertTrue(p['cash_only']);self.assertEqual(p['listed'],'listed')
 def test_related_prices_ignored(self):
  p=self.parse('<h1>2 bed house for sale</h1><p>£70,000</p><h2>Similar properties</h2><p>Grade II listed £99,000 Currently let at £900 pcm</p>')
  self.assertEqual(p['price'],70000);self.assertIsNone(p['rent']);self.assertEqual(p['listed'],'unknown')
 def test_jsonld(self):
  p=self.parse('<h1>3 bedroom house for sale</h1><script type="application/ld+json">{"@type":"House","numberOfBedrooms":3,"offers":{"price":85000,"priceCurrency":"GBP"}}</script><p>Freehold. Currently achieving £700 pcm.</p>')
  self.assertEqual(p['price'],85000);self.assertEqual(p['rent'],700)
 def test_let_listing_not_sale(self):self.assertIsNone(self.parse('<h1>2 bed house to rent £650 pcm</h1>'))
 def test_plural_bedrooms_and_asking_rent(self):
  p=self.parse('<meta property="og:title" content="For sale - 3 bedrooms, Vermont Villas, Hull - £90,000"><meta property="og:description" content="Recently marketed to let at £750 pcm"><p>Freehold.</p>')
  self.assertEqual(p['beds'],3);self.assertEqual(p['rent'],750);self.assertIn('not tenancy',p['rent_basis'])
 def test_benefit_from_modernisation(self):
  p=self.parse('<h1>2 bedroom house for sale £70,000</h1><p>Could benefit from some cosmetic modernisation.</p>')
  self.assertEqual(p['route'],'refurb')
 def test_urls_and_private_dns(self):
  for url in ['http://example.com/a','https://user:pass@example.com/a','https://example.com:8443/a']:
   with self.assertRaises(ValueError):canonical(url)
  with patch('collector.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
   with self.assertRaises(ValueError):public_url('https://example.com/a',['example.com'])
 def test_discovery_failure_still_checks_seed(self):
  fake=type('Fake',(),{'get':lambda self,url:(_ for _ in ()).throw(ValueError('blocked'))})()
  urls,errors=discover(fake,dict(seed_urls=['https://example.com/property/1'],discovery_urls=['https://example.com/sitemap.xml'],listing_pattern='/property/',domains=['example.com']))
  self.assertEqual(len(urls),1);self.assertEqual(len(errors),1)
 def test_robots_blocks_ai_path(self):
  f=Fetcher(['example.com'],0)
  with patch.object(f,'raw',return_value=('User-agent: GPTBot\nDisallow: /property/', 'text/plain')):
   with self.assertRaisesRegex(ValueError,'crawl rules'):f.get('https://example.com/property/1')

class PersistenceTests(unittest.TestCase):
 def test_change_history_and_restart(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'x.db');s.upsert(house());s.upsert(house(price=69000));s.upsert(house(price=69000))
   s2=Store(Path(d)/'x.db');self.assertEqual(len(s2.listings()),1)
   self.assertEqual(len([x for x in s2.changes() if x['kind']=='price']),1)
 def test_price_change_invalidates_mortgage_evidence(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'x.db');s.upsert(house(evidence=dict(mortgage='approved at 70k')))
   s.upsert(house(price=72000));self.assertNotIn('mortgage',s.listings()[0]['evidence'])
 def test_failed_fetch_not_fresh(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'x.db');p=house();s.upsert(p)
   source=dict(name='Failing agent',enabled=True,domains=['example.com'],listing_pattern='/property/',seed_urls=[p['url']])
   with patch('collector.Fetcher.get',side_effect=ValueError('blocked')):scan(s,[source],1)
   self.assertEqual(s.listing(p['url'])['observed_at'],p['observed_at']);self.assertEqual(s.scans()[0]['status'],'failed')
 def test_frontier_remembers_over_budget_pages(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'x.db');s.mark_checked('https://example.com/overpriced');self.assertIn('https://example.com/overpriced',Store(Path(d)/'x.db').checked_urls())

class HTTPTests(unittest.TestCase):
 def test_auth_csrf_and_health(self):
  with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'SCOUT_PASSWORD':'test-only-not-a-real-secret'}):
   app=Application(d);server=ThreadingHTTPServer(('127.0.0.1',0),handler(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   root=f'http://127.0.0.1:{server.server_port}'
   try:
    self.assertEqual(urlopen(root+'/healthz').status,200)
    with self.assertRaises(HTTPError) as ex:urlopen(root+'/api/state')
    self.assertEqual(ex.exception.code,401)
    auth='Basic '+base64.b64encode(b'owner:test-only-not-a-real-secret').decode()
    payload=json.loads(urlopen(Request(root+'/api/state',headers={'Authorization':auth})).read());self.assertEqual(len(payload['listings']),8)
    with self.assertRaises(HTTPError) as ex:urlopen(Request(root+'/api/settings',data=b'{}',headers={'Authorization':auth,'Content-Type':'application/json'}))
    self.assertEqual(ex.exception.code,403)
   finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
