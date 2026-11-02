"""Hourly read-only evaluator. Own storage/process; no broker order methods."""
import html
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime,timedelta,timezone
import requests
from etoro_feed import EtoroFeed,ASSET_UNIVERSE,UK_SYMBOLS
from broker_accounting import totals,all_positions,position_pnl,number
from bollinger_rsi import completed_daily_candles
from data_quality import age_seconds
from portfolio_evaluator import path_report,holding_diagnostics

STOP=threading.Event()
FED_SEED="https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm"
BOE="https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate"
SOFR="https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"

def atomic(path,payload):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload,allow_nan=False),encoding='utf-8');tmp.replace(path)

def rate_sources(now,previous):
    stamp=now.isoformat();today=now.date()
    rates={
     'USD':{'name':'Fed policy midpoint proxy','annual_pct':3.875,'as_of':'2026-10-28','verified_at':'2026-11-02',
            'valid_until':'2026-11-08','source':FED_SEED,'status':'VERIFIED_SEED_NOT_LIVE','day_basis':365},
     'GBP':{'name':'BoE Bank Rate proxy','annual_pct':3.75,'as_of':'2026-09-17','verified_at':'2026-11-02',
            'valid_until':'2026-11-04','source':BOE,'status':'VERIFIED_SEED_NOT_LIVE','day_basis':365}}
    for c,r in previous.items():
        if r.get('valid_until','')>=today.isoformat() and r.get('status') in ('SOURCE_FETCHED','CACHED_SOURCE'):rates[c]=dict(r,status='CACHED_SOURCE')
    errors={}
    try:
        response=requests.get(SOFR,timeout=12);response.raise_for_status();body=response.json()
        records=body.get('refRates',body.get('referenceRates',[]))
        record=next(r for r in records if r.get('type','SOFR')=='SOFR' and r['effectiveDate']<=today.isoformat())
        value=float(record['percentRate']);effective=datetime.fromisoformat(record['effectiveDate']).date()
        if not math.isfinite(value) or not -10<=value<=50 or (today-effective).days>7:raise ValueError('Invalid SOFR date/value')
        rates['USD']={'name':'SOFR overnight proxy','annual_pct':value,'as_of':effective.isoformat(),'verified_at':stamp,
                      'valid_until':(effective+timedelta(days=7)).isoformat(),'source':SOFR,'status':'SOURCE_FETCHED','day_basis':360}
    except Exception as error:errors['USD']=type(error).__name__
    try:
        response=requests.get(BOE,timeout=12);response.raise_for_status()
        text=html.unescape(re.sub('<[^>]+>',' ',response.text));text=re.sub(r'\s+',' ',text)
        match=re.search(r'Current\s+Bank\s+Rate\s*([-\d.]+)\s*%',text,re.I)
        if not match:raise ValueError('Current Bank Rate label missing')
        value=float(match[1])
        if not math.isfinite(value) or not -10<=value<=50:raise ValueError('Invalid Bank Rate')
        published=re.search(r'Published on (\d{1,2} \w+ \d{4})',text)
        effective=datetime.strptime(published[1],'%d %B %Y').date().isoformat() if published else today.isoformat()
        rates['GBP']={'name':'BoE Bank Rate proxy','annual_pct':value,'as_of':effective,'verified_at':stamp,
                      'valid_until':(today+timedelta(days=2)).isoformat(),'source':BOE,'status':'SOURCE_FETCHED','day_basis':365}
    except Exception as error:errors['GBP']=type(error).__name__
    for currency,r in rates.items():
        if r['valid_until']<today.isoformat():r.update(annual_pct=None,status='UNAVAILABLE_EXPIRED')
    return rates,errors

def read_main(db):
    c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    try:
        orders=[dict(r) for r in c.execute('SELECT * FROM demo_bot_orders')]
        execution=[]
        for o in orders:
            if o['status'] not in ('OPEN','CLOSED','CLOSING') or o.get('position_id') is None:continue
            q=c.execute('SELECT bid,ask,ts FROM quotes WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1',
                        (o['symbol'],o['created_at'])).fetchone()
            if not q:continue
            age=(datetime.fromisoformat(o['created_at'])-datetime.fromisoformat(q['ts'])).total_seconds()
            mid=(number(q['bid'])+number(q['ask']))/2
            if 0<=age<=180 and mid>0 and number(o['entry_price'])>0:
                execution.append({'symbol':o['symbol'],'strategy':o['strategy'],'amount_usd':o['amount_usd'],
                                  'mid_to_fill_bps':(o['entry_price']/mid-1)*10000,'quote_age_seconds':age,
                                  'method':'Nearest preceding persisted mid quote; reconstructed approximation including spread; excludes fees.'})
        return orders,execution
    finally:c.close()

def evaluate(db,folder,conn):
    now=datetime.now(timezone.utc)
    feed=EtoroFeed()
    portfolio=feed.fetch_portfolio('demo');account=totals(portfolio)
    if account['equity_usd']<=0:raise ValueError('Nonpositive equity cannot seed a growth account')
    cache=db.parent/'instrument_cache.json'
    mapping=json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    mapping={s:m for s,m in mapping.items() if isinstance(m,dict) and 'instrument_id' in m}
    by_id={int(m['instrument_id']):(s,m) for s,m in mapping.items()}
    groups={}
    allowed=set((ASSET_UNIVERSE['Stocks']+' '+ASSET_UNIVERSE['ETFs']+' '+ASSET_UNIVERSE['Crypto']+' '+ASSET_UNIVERSE['Commodities']).split())|set(UK_SYMBOLS)
    for p in all_positions(portfolio):
        instrument=int(p.get('instrumentID',p.get('instrumentId')))
        if instrument not in by_id:
            payload=feed.get('/market-data/search',{'instrumentId':instrument,'fields':'instrumentId,internalSymbolFull,displayname,internalAssetClassName','pageSize':5})
            record=next((r for r in payload.get('items',[]) if r.get('instrumentId')==instrument),None)
            if record:
                s=record['internalSymbolFull'];m={'instrument_id':instrument,'asset_class':record.get('internalAssetClassName','Unclassified')}
                mapping[s]=m;by_id[instrument]=(s,m)
            time.sleep(1.2)
        s,m=by_id.get(instrument,('ID:'+str(instrument),{'instrument_id':instrument,'asset_class':'Unknown'}))
        amount=number(p.get('amount',p.get('initialAmountInDollars')));pnl=position_pnl(p)
        h=groups.setdefault(s,{'symbol':s,'instrument_id':instrument,'asset_class':m['asset_class'],'amount_usd':0.,'value_usd':0.,'unrealized_pnl_usd':0.,'history_eligible':s in allowed})
        h['amount_usd']+=amount;h['value_usd']+=amount+pnl;h['unrealized_pnl_usd']+=pnl
        if number(p.get('leverage',1),1)!=1 or not p.get('isBuy',True):h['history_eligible']=False
    for s in ('SPY','QQQ','UK100','GBPUSD'):
        if s not in mapping:mapping.update(feed.resolve_symbols([s]));time.sleep(1.2)
    selected={s:mapping[s] for s in ('SPY','QQQ','UK100','GBPUSD') if s in mapping}
    quotes={q['symbol']:q for q in feed.fetch_rates(selected)}
    fxquote=quotes.get('GBPUSD') or {}
    fx=number(fxquote.get('last'),None)
    fxage=age_seconds(fxquote.get('quote_ts'),now)
    if fxage is None or not -5<=fxage<=7*86400:raise ValueError('GBPUSD reference stale or missing timestamp')
    if fx is None or fx<=0:raise ValueError('GBPUSD unavailable')
    previous=conn.execute('SELECT payload FROM observations ORDER BY stamp DESC LIMIT 1').fetchone()
    last=json.loads(previous[0]) if previous else {}
    rates,rate_errors=rate_sources(now,last.get('rates',{}))
    benchmarks={}
    for s in ('SPY','QQQ','UK100'):
        q=quotes.get(s) or {};value=number(q.get('last'),None)
        age=age_seconds(q.get('quote_ts'),now)
        if age is None or not -5<=age<=7*86400:value=None
        benchmarks[s+'_USD']=value*fx if s=='UK100' and value else value
    snapshot={'stamp':now.isoformat(),**account,'gbpusd':fx,'rates':rates,'benchmarks':benchmarks,
              'quote_dates':{s:q.get('quote_ts') for s,q in quotes.items()},'holdings':list(groups.values())}
    data={};failures={}
    history=folder/'candles';history.mkdir(exist_ok=True)
    wanted={h['symbol']:h['instrument_id'] for h in groups.values()}
    wanted.update({s:m['instrument_id'] for s,m in selected.items()})
    for s,instrument in wanted.items():
        file=history/(str(instrument)+'.json')
        try:
            if file.exists() and time.time()-file.stat().st_mtime<21600:bars=json.loads(file.read_text())
            else:
                bars=completed_daily_candles(feed.fetch_candles(instrument,count=500),now);atomic(file,bars);time.sleep(1.2)
            if not bars or (now-datetime.fromisoformat(bars[-1]['fromDate'])).total_seconds()>7*86400:
                raise ValueError('Historical candle series stale or empty')
            data[s]=bars
        except Exception as error:failures[s]=type(error).__name__
    flows_file=folder/'flows.json'
    if not flows_file.exists():atomic(flows_file,[])
    flows=json.loads(flows_file.read_text(encoding='utf-8'))
    for f in flows:
        if datetime.fromisoformat(f['stamp']).tzinfo is None or not math.isfinite(float(f['amount_usd'])):raise ValueError('Invalid external-flow ledger')
    observations=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM observations ORDER BY stamp')]+[snapshot]
    result=path_report(observations,flows)
    diagnostic=holding_diagnostics(snapshot['holdings'],data,data.get('GBPUSD',[]),account['equity_usd'])
    orders,execution=read_main(db)
    active={str(p.get('positionID',p.get('positionId'))):p for p in all_positions(portfolio)}
    bot_pnl=sum(position_pnl(active[str(o['position_id'])]) for o in orders
                if o.get('strategy')!='execution_check' and str(o.get('position_id')) in active)
    papers=json.loads(Path(__file__).with_name('research_methods.json').read_text(encoding='utf-8'))
    result.update(**diagnostic,latest_rates=rates,rate_fetch_errors=rate_errors,history_failures=failures,
                  execution_quality=execution,bot_strategy_open_pnl_usd=bot_pnl,papers=papers,
                  source='Fresh eToro demo P&L API; current holdings and balances. Market-data GETs only.',
                  limitations=[
                   'Growth starts at the new baseline. Older bot account-change baselines were not reconstructed.',
                   'Whole-account returns include manual and automated holdings. Bot open P&L is separately attributed; closed bot realized P&L is not inferred from last-seen marks.',
                   'External flows require an audited flows.json ledger. Empty ledger assumes no deposits/withdrawals/resets; selling an existing share is an internal trade.',
                   'Linked Modified Dietz intervals approximate time-weighted returns. GBP flow conversion uses prior observed FX. Missing observations and snapshots limit drawdown resolution.',
                   'Stock comparators are price-only SPY, QQQ and UK100, not dividend-reinvested total-return indices. UK100 is translated to USD with observed GBPUSD.',
                   'Cash curves use previously observed dated policy/SOFR proxies, not guaranteed deposit products or eToro interest payments. Expired rate data are unavailable, never zero-filled.',
                   'Current-holdings historical risk/momentum is a hypothetical fixed-weight diagnostic, not a historical return of this account. Costs, funding and corporate-action adjustments are not independently validated.',
                   'No alpha or full Fama-French/Carhart regression is claimed. This needs validated factors and a sufficiently long account return history.',
                   'GARCH grid fit and covariance are descriptive risk estimates, not live sizing inputs or dependable return forecasts.'])
    json.dumps(result,allow_nan=False)
    with conn:conn.execute('INSERT INTO observations VALUES(?,?)',(snapshot['stamp'],json.dumps(snapshot,allow_nan=False)))
    atomic(folder/'report.json',result)
    print('GROWTH_RESULT '+json.dumps({'phase':result['status'],'equity_usd':account['equity_usd'],'cash_usd':account['cash_usd'],
           'unrealized_pnl_usd':account['unrealized_pnl_usd'],'holdings':len(groups),'observations':len(observations),
           'rates':rates,'risk_status':diagnostic['risk']['status'],'history_failures':failures}),flush=True)

def main(db_text):
    db=Path(db_text);folder=db.parent/'growth';folder.mkdir(parents=True,exist_ok=True)
    lock=(folder/'worker.lock').open('a')
    try:
        import fcntl
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
    except ImportError:pass
    if hasattr(os,'nice'):os.nice(10)
    for s in (signal.SIGTERM,signal.SIGINT):signal.signal(s,lambda *_:STOP.set())
    conn=sqlite3.connect(folder/'growth.sqlite3');conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS observations(stamp TEXT PRIMARY KEY,payload TEXT NOT NULL)');conn.commit()
    interval=max(900,int(os.getenv('GROWTH_INTERVAL_SECONDS','3600')))
    try:
        while not STOP.is_set():
            last=conn.execute('SELECT stamp FROM observations ORDER BY stamp DESC LIMIT 1').fetchone()
            wait=0 if not last else interval-(datetime.now(timezone.utc)-datetime.fromisoformat(last[0])).total_seconds()
            if wait>0:
                if STOP.wait(min(wait,60)):break
                continue
            try:
                atomic(folder/'status.json',{'phase':'EVALUATING','updated_at':datetime.now(timezone.utc).isoformat()})
                evaluate(db,folder,conn)
                atomic(folder/'status.json',{'phase':'TRACKING','updated_at':datetime.now(timezone.utc).isoformat(),'interval_seconds':interval})
            except Exception as error:
                atomic(folder/'status.json',{'phase':'FAILED_RETRYING','error_type':type(error).__name__,'updated_at':datetime.now(timezone.utc).isoformat()})
                print('GROWTH_ERROR '+type(error).__name__,flush=True)
                if STOP.wait(60):break
    finally:conn.close();lock.close()

if __name__=='__main__':main(sys.argv[1])
