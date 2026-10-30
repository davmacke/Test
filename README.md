# eToro Market & Portfolio Analyst

A cloud-ready Streamlit dashboard using eToro's documented Public API. It polls quotes, builds daily-candle market signals, checks portfolio concentration and leverage, and estimates a risk-budget size for candidate entries.

## What it does

- Scans a configurable universe of eToro-discoverable crypto, commodities, currencies, indices, ETFs, and stocks. Unsupported symbols are skipped; the available instruments depend on eToro account and regional eligibility.
- Shows **Buy candidate**, **Hold / Watch**, or **Reduce / Avoid** signals from daily candles using 20/50-day moving averages, 20-day momentum, RSI, annualized volatility, 90-day drawdown, and ATR.
- Explains the components behind each signal. Historical analysis refreshes gradually to stay within eToro's shared market-data rate limits.
- Reads the selected eToro portfolio and flags concentration, leverage, low available cash, and positions without a recorded stop.
- Estimates a position size from a configurable risk budget and maximum account allocation.
- Continues to show the existing breadth and sector-relative-strength charts.

## Important limits

- This version is **read-only for eToro accounts**. It does not submit, edit, or close broker orders. Signals and size estimates are analysis outputs, not guaranteed results.
- The selected account defaults to **demo**. Set `ETORO_TRADING_MODE=real` only to read your real portfolio; it still cannot place trades.
- Risk thresholds are starting defaults, not personal financial advice. The default signal rules use a score of 4 or higher for Buy candidate and -2 or lower for Reduce / Avoid. Defaults for sizing are 0.5% of estimated account value at risk per candidate and a 5% maximum allocation.
- The estimated account value is available trading credit plus reported position capital. Gross exposure estimates position capital multiplied by leverage. These figures may differ from eToro's current equity and liquidation values.
- REST quote snapshots are not guaranteed exchange-direct tick streaming. Closed markets may show stale prices.
- No credential values or local database files should be committed to GitHub.

## Render deployment

1. In Render, create or update the Blueprint using `render.yaml`.
2. Set `ETORO_API_KEY` and `ETORO_USER_KEY` as secret environment variables in Render. Never paste them into chat or commit them.
3. Keep `ETORO_TRADING_MODE` set to `demo` initially.
4. Deploy and inspect the **Feed status** tab and Render logs for API-scope, rate-limit, and unsupported-symbol messages.
5. Initial symbol discovery is throttled and daily candles are fetched in small batches. Signals appear as history becomes available.

Optional environment settings:

- `UNIVERSE_LIMIT` (20–100; default 100)
- `POLL_SECONDS` (minimum 30; default 60)
- `ANALYSIS_REFRESH_SECONDS` (default 1800)
- `CANDLES_PER_CYCLE` (default 10)
- `PORTFOLIO_REFRESH_SECONDS` (default 300)
- `RISK_PER_TRADE_PCT` (default 0.5)
- `MAX_POSITION_PCT` (default 5)

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
