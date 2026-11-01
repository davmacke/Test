"""Read-only historical backtest worker. Never imports demo_trader or calls trading APIs."""
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from bollinger_rsi import completed_daily_candles
from etoro_feed import EtoroFeed, DEFAULT_SYMBOLS
from backtest import replay

def atomic(path,payload):
    temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload,allow_nan=False),encoding="utf-8")
    temp.replace(path)

def main(db_path,token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",token):
        raise ValueError("Invalid backtest identifier")
    folder=Path(db_path).parent/"backtests"/token
    folder.mkdir(parents=True,exist_ok=True)
    lock=(folder/"worker.lock").open("a")
    try:
        import fcntl
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
    except ImportError:
        pass
    if (folder/"report.json").exists(): return
    try:
        if hasattr(os,"nice"): os.nice(10)
        old={}
        if (folder/"status.json").exists():
            old=json.loads((folder/"status.json").read_text())
        as_of=old.get("as_of") or datetime.now(timezone.utc).isoformat()
        snapshot=datetime.fromisoformat(as_of)
        def status(phase,**extra):
            atomic(folder/"status.json",{"id":token,"phase":phase,"as_of":as_of,
                "updated_at":datetime.now(timezone.utc).isoformat(),**extra})
            print("BACKTEST_PROGRESS "+json.dumps({"id":token,"phase":phase,**extra}),flush=True)
        mapping_path=Path(db_path).parent/"instrument_cache.json"
        if not mapping_path.exists():
            status("WAITING",message="Waiting for the market worker's instrument mapping")
            return
        mapping=json.loads(mapping_path.read_text())
        wanted=DEFAULT_SYMBOLS[:max(20,min(100,int(os.getenv("UNIVERSE_LIMIT","100"))))]
        mapping={s:mapping[s] for s in wanted if isinstance(mapping.get(s),dict) and "instrument_id" in mapping[s]}
        if not mapping: raise ValueError("No resolved instruments for historical replay")
        feed=EtoroFeed()
        data={}; classes={}; failures={}
        status("FETCHING",instruments=len(mapping))
        for index,(symbol,meta) in enumerate(mapping.items()):
            file=folder/(str(int(meta["instrument_id"]))+".json")
            try:
                if file.exists(): rows=json.loads(file.read_text())
                else:
                    rows=completed_daily_candles(feed.fetch_candles(meta["instrument_id"],count=1000),snapshot)
                    atomic(file,rows)
                    time.sleep(1)
                rows=completed_daily_candles(rows,snapshot)
                if len(rows)<80: raise ValueError("Less than 80 completed daily candles")
                data[symbol]=rows; classes[symbol]=meta.get("asset_class","Unclassified")
            except Exception as error:
                # Credentials, request headers and server response bodies are never logged.
                failures[symbol]=type(error).__name__
            if index%10==0 or index==len(mapping)-1:
                status("FETCHING",done=index+1,total=len(mapping),usable=len(data),failures=failures)
        capital=float(os.getenv("BACKTEST_INITIAL_USD","10000"))
        if not 0<capital<1e9: raise ValueError("Invalid simulated initial capital")
        def progress(done,total):
            status("REPLAYING",done=done,total=total,usable=len(data))
        report=replay(data,classes,capital,progress)
        report.update(id=token,as_of=as_of,finished_at=datetime.now(timezone.utc).isoformat(),
            data_source="eToro daily candle API, up to 1000 completed bars per current-universe instrument",
            data_failures=failures,initial_capital_usd=capital)
        atomic(folder/"report.json",report)
        for summary in report["summaries"]:
            print("BACKTEST_RESULT "+json.dumps({"id":token,**summary}),flush=True)
        status("COMPLETE",usable=len(data),results=len(report["summaries"]))
    except Exception as error:
        atomic(folder/"status.json",{"id":token,"phase":"FAILED",
            "updated_at":datetime.now(timezone.utc).isoformat(),"error_type":type(error).__name__})
        print("BACKTEST_FAILED "+json.dumps({"id":token,"error_type":type(error).__name__}),flush=True)
        raise
    finally:
        lock.close()

if __name__=="__main__": main(sys.argv[1],sys.argv[2])
