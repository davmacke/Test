"""Isolated disclosure research. eToro market-data GETs only; never imports trading."""
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime,timezone
from bollinger_rsi import completed_daily_candles
from etoro_feed import EtoroFeed
from copy_backtest import replay_copy,qualifying

def atomic(path,payload):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload,allow_nan=False),encoding='utf-8'); temp.replace(path)

def main(db_path,token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',token): raise ValueError('Invalid copy job ID')
    root=Path(db_path).parent; folder=root/'copy_research'/token; folder.mkdir(parents=True,exist_ok=True)
    lock=(folder/'worker.lock').open('a')
    try:
        import fcntl
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
    except ImportError: pass
    if (folder/'report.json').exists(): return
    def status(phase,**extra):
        obj={'id':token,'phase':phase,'updated_at':datetime.now(timezone.utc).isoformat(),**extra}
        atomic(folder/'status.json',obj); print('COPY_PROGRESS '+json.dumps(obj),flush=True)
    try:
        if hasattr(os,'nice'): os.nice(10)
        dataset=os.getenv('COPY_DATASET_ID','historical-20261101-a')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',dataset): raise ValueError('Invalid dataset')
        source=root/'backtests'/dataset
        original=json.loads((source/'status.json').read_text())
        as_of=original['as_of']; snapshot=datetime.fromisoformat(as_of)
        ledger=json.loads(Path(__file__).with_name('copy_disclosures.json').read_text(encoding='utf-8'))
        wanted={'SPY','GBPUSD'}|{h['ticker'] for f in ledger['berkshire'] for h in f['holdings'] if h.get('ticker')}
        wanted|={r['ticker'] for r in ledger['pelosi'] if qualifying(r)[0]}
        mapping=json.loads((root/'instrument_cache.json').read_text())
        # Supplemental cache belongs to this study, never changes the demo bot's instrument universe.
        supplements=folder/'mapping.json'
        if supplements.exists(): mapping.update(json.loads(supplements.read_text()))
        feed=EtoroFeed(); data={}; failures={}; coverage={}
        status('FETCHING',total=len(wanted),as_of=as_of)
        for i,s in enumerate(sorted(wanted)):
            try:
                meta=mapping.get(s)
                if not isinstance(meta,dict) or 'instrument_id' not in meta:
                    found=feed.resolve_symbols([s]); meta=found.get(s)
                    if meta is None: raise ValueError('No exact broker symbol')
                    mapping.update(found); atomic(supplements,{k:v for k,v in mapping.items() if k in wanted})
                    time.sleep(1.2)
                cached=source/(str(int(meta['instrument_id']))+'.json')
                own=folder/(s+'.json')
                if own.exists(): rows=json.loads(own.read_text())
                elif cached.exists(): rows=json.loads(cached.read_text())
                else:
                    rows=completed_daily_candles(feed.fetch_candles(meta['instrument_id'],count=1000),snapshot)
                    atomic(own,rows); time.sleep(1.2)
                rows=completed_daily_candles(rows,snapshot)
                if not rows: raise ValueError('No valid historical bars')
                data[s]=rows
                coverage[s]={'bars':len(rows),'first':rows[0]['fromDate'],'last':rows[-1]['fromDate']}
            except Exception as error: failures[s]=type(error).__name__
            status('FETCHING',done=i+1,total=len(wanted),failures=failures)
        if len(data.get('SPY',[]))<500: raise ValueError('SPY calendar too short')
        status('REPLAYING',instruments=len(data),failures=failures)
        result=replay_copy(data,ledger,float(os.getenv('COPY_INITIAL_GBP','1250')))
        result.update(id=token,as_of=as_of,dataset=dataset,coverage=coverage,data_failures=failures,
                      finished_at=datetime.now(timezone.utc).isoformat(),revision='disclosure-followers-v1')
        atomic(folder/'report.json',result)
        for s in result['summaries']: print('COPY_RESULT '+json.dumps({'id':token,**s}),flush=True)
        status('COMPLETE',comparisons=len(result['summaries']),failures=failures,
               qualified_pelosi=sum(qualifying(r)[0] for r in ledger['pelosi']))
    except Exception as error:
        status('FAILED',error_type=type(error).__name__); raise
    finally: lock.close()

if __name__=='__main__': main(sys.argv[1],sys.argv[2])
