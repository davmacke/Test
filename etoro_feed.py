import os
import time
import uuid
import requests
import math
from datetime import datetime, timezone
from data_quality import utc_timestamp

BASE = "https://public-api.etoro.com/api/v1"

ASSET_UNIVERSE = {
    "Crypto": "BTC ETH XRP SOL ADA DOGE LTC BCH LINK AVAX DOT UNI",
    "Commodities": "GOLD SILVER OIL NATGAS COPPER PLATINUM",
    "Currencies": "EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD EURGBP GBPJPY",
    "Indices": "SPX500 NSDQ100 DJ30 GER40 UK100 FRA40 JPN225 AUS200",
    "ETFs": "SPY QQQ DIA IWM RSP XLK XLF XLE XLI XLV XLY SMH SOXX GLD SLV TLT HYG USO XOP VOO VTI ARKK EEM EFA VNQ",
    "Stocks": "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD INTC QCOM MU ORCL CRM ADBE NFLX PLTR PANW JPM BAC WFC GS MS V MA AXP COIN HOOD XOM CVX COP SLB OXY EOG PSX VLO CAT DE GE HON RTX LMT NOC BA UPS FDX WMT COST TGT HD LOW MCD SBUX KO PEP PG LLY JNJ MRK ABBV PFE UNH ISRG ABT TMO DIS CMCSA T VZ TMUS UBER ABNB BKNG SHOP",
}
DEFAULT_SYMBOLS = list(dict.fromkeys(
    symbol for symbols in ASSET_UNIVERSE.values() for symbol in symbols.split()
))


class EtoroFeed:
    def __init__(self):
        self.api = os.getenv("ETORO_API_KEY", "").strip()
        self.user = os.getenv("ETORO_USER_KEY", "").strip()
        if not self.api or not self.user:
            raise RuntimeError("Missing eToro API credentials")
        self.session = requests.Session()

    def get(self, path, params=None):
        for attempt in range(5):
            response = self.session.get(
                BASE + path,
                headers={
                    "Accept": "application/json",
                    "x-api-key": self.api,
                    "x-user-key": self.user,
                    "x-request-id": str(uuid.uuid4()),
                },
                params=params,
                timeout=25,
            )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Rate limit persisted after retries")

    def resolve_symbols(self, symbols):
        found = {}
        for index, symbol in enumerate(symbols):
            payload = self.get("/market-data/search", {
                "internalSymbolFull": symbol,
                "fields": "instrumentId,internalSymbolFull,displayname,marketId,internalAssetClassName,instrumentType,isCurrentlyTradable,isBuyEnabled,isOpen,isExchangeOpen",
                "pageSize": 5,
            })
            rows = payload.get("data", payload.get("results", payload.get("items", [])))
            if isinstance(rows, dict):
                rows = rows.get("items", [])
            for row in rows if isinstance(rows, list) else []:
                resolved = row.get("internalSymbolFull", row.get("symbol"))
                instrument_id = row.get("instrumentId", row.get("instrumentID"))
                if resolved and str(resolved).upper() == symbol.upper() and instrument_id is not None:
                    asset_class = (
                        row.get("internalAssetClassName")
                        or row.get("instrumentType")
                        or "Unclassified"
                    )
                    found[symbol] = {
                        "instrument_id": int(instrument_id),
                        "display_name": row.get("displayname", symbol),
                        "asset_class": str(asset_class),
                        "is_currently_tradable": row.get("isCurrentlyTradable"),
                        "is_buy_enabled": row.get("isBuyEnabled"),
                        "is_open": row.get("isOpen") if row.get("isOpen") is not None else row.get("isExchangeOpen"),
                        "open_status_source": "isOpen" if row.get("isOpen") is not None else (
                            "isExchangeOpen" if row.get("isExchangeOpen") is not None else "unavailable"
                        ),
                        "status_checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                    break
            if index < len(symbols) - 1:
                time.sleep(1.2)
        # Unresolved symbols are cached by the caller; HTTP/API failures still raise.
        return found

    def fetch_rates(self, mapping):
        pairs = list(mapping.items())
        output = []
        for start in range(0, len(pairs), 25):
            batch = pairs[start:start + 25]
            ids = ",".join(str(meta["instrument_id"]) for _, meta in batch)
            data = self.get("/market-data/instruments/rates", {"instrumentIds": ids})
            rows = data.get("rates", data.get("data", []))
            if isinstance(rows, dict):
                rows = rows.get("rates", rows.get("items", []))
            by_id = {
                int(row.get("instrumentId", row.get("instrumentID"))): row
                for row in rows if isinstance(row, dict)
                and row.get("instrumentId", row.get("instrumentID")) is not None
            }
            for symbol, meta in batch:
                row = by_id.get(meta["instrument_id"])
                if row:
                    def number(key):
                        try:
                            value = float(row[key])
                            return value if math.isfinite(value) else None
                        except (KeyError, TypeError, ValueError, OverflowError):
                            return None
                    broker_stamp = utc_timestamp(row.get("date") or row.get("timestamp"))
                    last = next((number(key) for key in ("lastPrice", "lastExecution", "rate")
                                 if number(key) is not None and number(key) > 0), None)
                    if last is None and number("bid") and number("ask"):
                        last = (number("bid") + number("ask")) / 2
                    output.append({
                        "symbol": symbol,
                        "instrument_id": meta["instrument_id"],
                        "asset_class": meta.get("asset_class", "Unclassified"),
                        "last": last,
                        "quote_ts": broker_stamp.isoformat() if broker_stamp else None,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "bid": number("bid"),
                        "ask": number("ask"),
                        "change_pct": number("change"),
                    })
        if not output:
            raise RuntimeError("Rates API returned no usable quotes")
        return output

    def fetch_candles(self, instrument_id, interval="OneDay", count=100):
        count = max(50, min(1000, int(count)))
        payload = self.get(
            f"/market-data/instruments/{int(instrument_id)}/history/candles/asc/{interval}/{count}"
        )
        groups = payload.get("candles", [])
        if isinstance(groups, dict):
            groups = groups.get("candles", [])
        candles = []
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("candles"), list):
                    candles.extend(group["candles"])
                elif isinstance(group, dict) and group.get("close") is not None:
                    candles.append(group)
        return candles

    def fetch_portfolio(self, mode="demo"):
        mode = str(mode).lower()
        if mode == "real":
            path = "/trading/info/portfolio"
        else:
            path = "/trading/info/demo/pnl"
        return self.get(path)

    def _headers(self, request_id=None):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.api,
            "x-user-key": self.user,
            "x-request-id": request_id or str(uuid.uuid4()),
        }

    def place_demo_order(self, order, request_id):
        url = "https://public-api.etoro.com/api/v2/trading/execution/demo/orders"
        for attempt in range(3):
            response = self.session.post(
                url, headers=self._headers(request_id), json=order, timeout=25
            )
            if response.status_code == 429:
                time.sleep(min(int(response.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Demo order rate limit persisted after retries")

    def lookup_demo_order(self, order_id=None, reference_id=None):
        if bool(order_id) == bool(reference_id):
            raise ValueError("Provide exactly one of order_id or reference_id")
        url = "https://public-api.etoro.com/api/v2/trading/info/demo/orders:lookup"
        params = {"orderId": order_id} if order_id else {"referenceId": reference_id}
        response = self.session.get(url, headers=self._headers(), params=params, timeout=25)
        response.raise_for_status()
        return response.json()

    def close_demo_position(self, position_id, instrument_id, request_id):
        url = (
            BASE + f"/trading/execution/demo/market-close-orders/positions/{int(position_id)}"
        )
        response = self.session.post(
            url,
            headers=self._headers(request_id),
            json={"InstrumentID": int(instrument_id)},
            timeout=25,
        )
        response.raise_for_status()
        return response.json()

    def cancel_demo_order(self, order_id, request_id):
        url = (
            "https://public-api.etoro.com/api/v2/trading/execution/demo/orders/"
            + str(int(order_id))
        )
        response = self.session.delete(url, headers=self._headers(request_id), timeout=25)
        response.raise_for_status()
        return response.json() if response.content else {}
