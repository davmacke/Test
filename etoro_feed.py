import os, time, uuid, requests

BASE = "https://public-api.etoro.com/api/v1"
DEFAULT_SYMBOLS = """BTC ETH XRP SOL ADA DOGE LTC BCH LINK AVAX DOT UNI AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD INTC QCOM MU ORCL CRM ADBE NFLX PLTR PANW JPM BAC WFC GS MS V MA AXP COIN HOOD XOM CVX COP SLB OXY EOG PSX VLO CAT DE GE HON RTX LMT NOC BA UPS FDX WMT COST TGT HD LOW MCD SBUX KO PEP PG LLY JNJ MRK ABBV PFE UNH ISRG ABT TMO DIS CMCSA T VZ TMUS UBER ABNB BKNG SHOP SPY RSP QQQ DIA IWM XLK XLF XLE XLI XLV XLY SMH SOXX GLD SLV TLT HYG USO XOP""".split()

class EtoroFeed:
    def __init__(self):
        self.api = os.getenv("ETORO_API_KEY", "").strip()
        self.user = os.getenv("ETORO_USER_KEY", "").strip()
        if not self.api or not self.user:
            raise RuntimeError("Missing eToro API credentials")
        self.session = requests.Session()

    def get(self, path, params=None):
        for attempt in range(5):
            r = self.session.get(BASE+path, headers={"Accept":"application/json","x-api-key":self.api,
                "x-user-key":self.user,"x-request-id":str(uuid.uuid4())}, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(min(int(r.headers.get("Retry-After", 2**attempt)), 60))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Rate limit persisted after retries")

    def resolve_symbols(self, symbols):
        found = {}
        for i, symbol in enumerate(symbols):
            payload = self.get("/market-data/search", {"internalSymbolFull":symbol,
                "fields":"instrumentId,internalSymbolFull,displayname,marketId","pageSize":5})
            rows = payload.get("data", payload.get("results", []))
            if isinstance(rows, dict): rows = rows.get("items", [])
            for row in rows if isinstance(rows, list) else []:
                sym = row.get("internalSymbolFull", row.get("symbol"))
                iid = row.get("instrumentId", row.get("instrumentID"))
                if sym == symbol and iid is not None:
                    found[symbol] = {"instrument_id":int(iid), "display_name":row.get("displayname",symbol)}
                    break
            if i < len(symbols)-1: time.sleep(1.2)
        if not found: raise RuntimeError("No instruments resolved; check eToro API scopes/response schema")
        return found

    def fetch_rates(self, mapping):
        pairs = list(mapping.items())
        out = []
        for start in range(0, len(pairs), 25):
            batch = pairs[start:start+25]
            ids = ",".join(str(meta["instrument_id"]) for _,meta in batch)
            data = self.get("/market-data/instruments/rates", {"instrumentIds":ids})
            rows = data.get("rates", data.get("data", []))
            if isinstance(rows, dict): rows = rows.get("rates", [])
            by_id = {int(x.get("instrumentId",x.get("instrumentID"))):x for x in rows
                     if isinstance(x,dict) and x.get("instrumentId",x.get("instrumentID")) is not None}
            for symbol,meta in batch:
                row = by_id.get(meta["instrument_id"])
                if row:
                    def num(k):
                        return float(row[k]) if row.get(k) is not None else None
                    out.append({"symbol":symbol,"instrument_id":meta["instrument_id"],
                        "last":num("lastPrice") if row.get("lastPrice") is not None else num("rate"),
                        "bid":num("bid"),"ask":num("ask"),"change_pct":num("change")})
        if not out: raise RuntimeError("Rates API returned no usable quotes")
        return out
