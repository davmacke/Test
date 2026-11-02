"""Independent news/research worker; no order permissions or trading calls."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
from datetime import datetime,timezone
import requests
from etoro_feed import EtoroFeed
from news_research import collect,staged_replay

STOP=threading.Event()
def atomic(path,payload):
    temp=path.with_suffix(".tmp");temp.write_text(json.dumps(payload,allow_nan=False),encoding="utf-8");temp.replace(path)

def shell_research(db,now):
    cache=db.parent/"instrument_cache.json"
    mapping=json.loads(cache.read_text()) if cache.exists() else {}
    shell=mapping.get("SHEL.L") or {}
    instrument=shell.get("instrument_id")
    if not instrument:return {"status":"SHELL_INSTRUMENT_UNRESOLVED"}
    file=db.parent/"growth"/"candles"/(str(instrument)+".json")
    if file.exists():bars=json.loads(file.read_text())
    else:bars=EtoroFeed().fetch_candles(instrument,count=500)
    return staged_replay(bars,now)

def main(db_text):
    db=Path(db_text);folder=db.parent/"news";folder.mkdir(parents=True,exist_ok=True)
    lock=(folder/"worker.lock").open("a")
    try:
        import fcntl
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
    except ImportError:pass
    if hasattr(os,"nice"):os.nice(10)
    for name in (signal.SIGINT,signal.SIGTERM):signal.signal(name,lambda *_:STOP.set())
    c=sqlite3.connect(folder/"news.sqlite3");c.execute("PRAGMA journal_mode=WAL")
    session=requests.Session()
    def paced_get(*args,**kwargs):
        try:return session.get(*args,**kwargs)
        finally:STOP.wait(6)
    interval=max(900,int(os.getenv("NEWS_INTERVAL_SECONDS","900")))
    try:
        while not STOP.is_set():
            now=datetime.now(timezone.utc)
            try:
                atomic(folder/"status.json",{"phase":"FETCHING","updated_at":now.isoformat()})
                report=collect(c,paced_get,now)
                try:report["staged_shell"]=shell_research(db,now)
                except Exception as error:report["staged_shell"]={"status":"FAILED_RETRYING","error":type(error).__name__}
                report["interval_seconds"]=interval
                atomic(folder/"report.json",report)
                atomic(folder/"status.json",{"phase":report["status"],"updated_at":datetime.now(timezone.utc).isoformat()})
                print("NEWS_RESULT "+json.dumps({"status":report["status"],"sources":report["sources"],
                    "new":report["new_articles"],"stored":len(report["articles"]),
                    "shell_research":report["staged_shell"]["status"]}),flush=True)
            except Exception as error:
                atomic(folder/"status.json",{"phase":"FAILED_RETRYING","error":type(error).__name__,"updated_at":now.isoformat()})
                print("NEWS_ERROR "+type(error).__name__,flush=True)
            if STOP.wait(interval):break
    finally:session.close();c.close();lock.close()
if __name__=="__main__":main(sys.argv[1])
