"""Read-only research worker using an existing frozen historical dataset."""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS","1")
os.environ.setdefault("OMP_NUM_THREADS","1")
import json
from pathlib import Path
import re
import sys
from datetime import datetime,timezone
import numpy
import statsmodels
from research_backtest import replay_research

def atomic(path,payload):
    temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload,allow_nan=False),encoding="utf-8")
    temp.replace(path)

def main(db_path,token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",token): raise ValueError("Invalid research ID")
    root=Path(db_path).parent
    folder=root/"research"/token
    folder.mkdir(parents=True,exist_ok=True)
    lock=(folder/"worker.lock").open("a")
    try:
        import fcntl
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
    except ImportError: pass
    if (folder/"report.json").exists(): return
    def status(phase,**extra):
        atomic(folder/"status.json",{"id":token,"phase":phase,"updated_at":datetime.now(timezone.utc).isoformat(),**extra})
        print("RESEARCH_PROGRESS "+json.dumps({"id":token,"phase":phase,**extra}),flush=True)
    try:
        if hasattr(os,"nice"): os.nice(10)
        dataset=os.getenv("RESEARCH_DATASET_ID","historical-20261101-a")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",dataset): raise ValueError("Invalid dataset ID")
        source=root/"backtests"/dataset
        mapping=json.loads((root/"instrument_cache.json").read_text())
        data={}
        for symbol,meta in mapping.items():
            if not isinstance(meta,dict) or "instrument_id" not in meta: continue
            file=source/(str(int(meta["instrument_id"]))+".json")
            if file.exists():
                rows=json.loads(file.read_text())
                if rows: data[symbol]=rows
        original=json.loads((source/"status.json").read_text())
        status("REPLAYING",instruments=len(data),as_of=original.get("as_of"))
        def progress(done,total): status("REPLAYING",done=done,total=total)
        result=replay_research(data,capital_gbp=float(os.getenv("RESEARCH_INITIAL_GBP","1250")),progress=progress)
        result.update(id=token,dataset=dataset,as_of=original.get("as_of"),
            finished_at=datetime.now(timezone.utc).isoformat(),numpy_version=numpy.__version__,
            statsmodels_version=statsmodels.__version__,data_source="Frozen eToro completed daily candles",
            strategy_revision="three-images-v1")
        atomic(folder/"report.json",result)
        for summary in result["summaries"]:
            print("RESEARCH_RESULT "+json.dumps({"id":token,**summary}),flush=True)
        print("RESEARCH_DIAGNOSTICS "+json.dumps({"id":token,
            "neural":result["neural_diagnostics"],"accepted_pair_folds":sum(p["accepted"] for p in result["pair_tests"]),
            "tested_pair_folds":len(result["pair_tests"]),"fx":result["gbpusd"],
            "fx_date":result["fx_date"],"coverage":len(result["coverage"])}),flush=True)
        status("COMPLETE",results=len(result["summaries"]))
    except Exception as error:
        status("FAILED",error_type=type(error).__name__)
        raise
    finally: lock.close()

if __name__=="__main__": main(sys.argv[1],sys.argv[2])
