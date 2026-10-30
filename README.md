# eToro Market & Portfolio Analyst

A cloud-ready Streamlit dashboard using eToro's documented Public API. It polls quotes, builds daily-candle market signals, checks portfolio concentration and leverage, and estimates a risk-budget size for candidate entries.

## What it does

- Scans a configurable universe of eToro-discoverable crypto, commodities, currencies, indices, ETFs, and stocks. Unsupported symbols are skipped; the available instruments depend on eToro account and regional eligibility.
- Shows **Buy candidate**, **Hold / Watch**, or **Reduce / Avoid** signals from daily candles using 20/50-day moving averages, 20-day momentum, RSI, annualized volatility, 90-day drawdown, and ATR.
- Explains the components behind each signal. Historical analysis refreshes gradually to stay within eToro's shared market-data rate limits.
- Reads the selected eToro portfolio and flags concentration, leverage, low available cash, and positions without a recorded stop.
- Estimates a position size from a configurable risk budget and maximum account allocation.
- Can automatically trade eToro **demo only** for a bounded 8-hour run: long-only, 1x leverage, at most 5 entries, at most 50 USD per entry, and at most 5 open positions. Each entry includes a broker-side ATR stop and 2R target.
- Closes only positions opened by this bot when the signal turns Reduce / Avoid or the run window ends. Stops new entries if bot open-position P&L reaches -25 USD.
- Continues to show the existing breadth and sector-relative-strength charts and a demo order/P&L log.

## Important limits

- Automatic execution is hard-locked to eToro's **demo order endpoint**. It never sends an order to the real account. `ETORO_TRADING_MODE` must remain `demo` for the bot to run; selecting `real` disables order execution.
- The bot's signals and size estimates are rule outputs, not guaranteed results. Demo fills, fees, spreads, and overnight performance may differ from live trading.
- Risk thresholds are starting defaults, not personal financial advice. The default signal rules use a score of 4 or higher for Buy candidate and -2 or lower for Reduce / Avoid. Defaults for sizing are 0.5% of estimated account value at risk per candidate and a 5% maximum allocation.
- The estimated account value is available trading credit plus reported position capital. Gross exposure estimates position capital multiplied by leverage. These figures may differ from eToro's current equity and liquidation values.
- REST quote snapshots are not guaranteed exchange-direct tick streaming. Closed markets may show stale prices.
- No credential values or local database files should be committed to GitHub.

## Render deployment

1. In Render, create or update the Blueprint using `render.yaml`.
2. Set `ETORO_API_KEY` and `ETORO_USER_KEY` as secret environment variables in Render. Never paste them into chat or commit them.
3. Keep `ETORO_TRADING_MODE` set to `demo` initially.
4. Keep the service on a continuously running plan with persistent storage. Render Free services can spin down after 15 minutes idle and lose local database state on restart; they are not suitable for unattended trading.
5. Confirm the eToro API keys include demo read and demo trade scopes. Keep `ETORO_TRADING_MODE=demo`.
6. Deploy and inspect **Feed status** and **Demo bot** tabs plus Render logs. Initial symbol discovery and daily candle history load gradually.

Optional environment settings:

- `UNIVERSE_LIMIT` (20–100; default 100)
- `POLL_SECONDS` (minimum 30; default 60)
- `ANALYSIS_REFRESH_SECONDS` (default 1800)
- `CANDLES_PER_CYCLE` (default 10)
- `PORTFOLIO_REFRESH_SECONDS` (default 300)
- `RISK_PER_TRADE_PCT` (default 0.5)
- `MAX_POSITION_PCT` (default 5)
- `DEMO_AUTOTRADE_ENABLED` (default false in code; true in the Render Blueprint)
- `DEMO_RUN_HOURS` (default 8; capped at 12)
- `DEMO_MAX_ORDERS_PER_RUN` (default 5; capped at 5)
- `DEMO_MAX_OPEN_POSITIONS` (default 5; capped at 5)
- `DEMO_MAX_ORDER_USD` (default 50; capped at 100)
- `DEMO_MAX_OPEN_LOSS_USD` (default 25)

The default universe is a starting list. To change it, edit `ASSET_UNIVERSE` in `etoro_feed.py`; only symbols returned by eToro are used.

## Local run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ETORO_API_KEY="your-key"
$env:ETORO_USER_KEY="your-user-key"
streamlit run app.py
```

## Existing relative-strength view

- `RSP / SPY`: equal-weight versus cap-weight S&P 500 leadership.
- `XLV / SPY`, `XLK / SPY`, `XLF / SPY`, `XLE / SPY`, `XLI / SPY`, `XLY / SPY`, and `SMH / SPY`: sector or industry ETFs versus SPY.
- Ratios are normalized to 100 from the first observation and the 24-hour comparison only appears after enough stored history exists.
