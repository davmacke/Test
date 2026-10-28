# eToro Cloud Paper Scanner

A cloud-ready Streamlit starter using eToro's documented Market Data REST API. It discovers symbols, requests current rate snapshots in batches, records quotes in SQLite and presents a web dashboard, including equal-weight breadth and sector relative-strength ratios.

## Safety
- Paper-only: no broker order placement code exists.
- New paper entries are disabled in this initial cloud version; existing open simulated positions can be evaluated for stop/target based on refreshed prices.
- Existing local SQLite positions are not migrated automatically.
- REST rate snapshots are current quotes, not guaranteed exchange-direct tick streaming.
- Never commit credentials or local database files.

## Deploy
1. Create a **private GitHub repository** and upload these files only.
2. In Render, select **New > Blueprint**, connect the repository, and deploy `render.yaml`.
3. Add `ETORO_API_KEY` and `ETORO_USER_KEY` in Render's environment settings.
4. Open the HTTPS URL Render provides. Inspect Recent events for discovery, authentication or quote errors.
5. The Render Starter web service and persistent disk are paid. Check current pricing before deployment.

## Breadth and sector leadership
- `RSP / SPY`: equal-weight S&P 500 versus cap-weighted S&P 500 leadership.
- `XLV / SPY`, `XLK / SPY`, `XLF / SPY`, `XLE / SPY`, `XLI / SPY`, `XLY / SPY`, and `SMH / SPY`: healthcare, technology, financials, energy, industrials, consumer discretionary, and semiconductors versus SPY.
- The dashboard stores each ratio from the same polling cycle, normalises the first observation to 100, and charts the relative-strength index with approximate 1-hour and 24-hour changes once enough history exists. It does not generate trades from these ratios.
- Historical depth starts at deployment and builds over time; the 24-hour comparison will not appear until at least 24 hours of observations have accumulated.

## Universe
Default watchlist starts with 12 crypto assets (24/7 market availability) followed by US stocks and ETFs, with up to 100 symbols total. Set `UNIVERSE_LIMIT` between 20 and 100. Instrument resolution is throttled at about one request/second to respect API limits; first startup may take a couple of minutes. Unsupported instruments are skipped, not assigned invented IDs. Rates are polled in batches of 25.

## Local test
```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
$env:ETORO_API_KEY="your-key"
$env:ETORO_USER_KEY="your-user-key"
streamlit run app.py
```

## Loading-screen troubleshooting
The Render start command disables Streamlit CORS and WebSocket compression to help diagnose proxy/WebSocket loading issues. If the page still stays on its loading skeleton, inspect Render runtime logs for the first traceback and verify the service environment variables. Closed US stock markets do not themselves prevent the dashboard page from loading.
