# Independent demo trade approval

The strategy scanner proposes entries. A separate deterministic approval module has veto authority before every new demo strategy order and before legacy demo execution probes. It cannot place or close orders. It runs in the same service process to avoid an asynchronous/stale approval race and adds no LLM/API subscription cost.

## Version market_approval_v1
- All strategies, including Bollinger/RSI, need APPROVED. WAIT and VETO prevent submission. Existing risk sizing, broker permission/quote checks, stops, targets, exit rules and Friday 6 November 2026 21:00 UTC expiry remain.
- Use 55+ completed daily OHLC bars. Daily fetch must be at most two hours old, last completed bar at most five calendar days old, and benchmark/sector/basket dates must match. Incomplete bars are excluded. Missing evidence waits; it is never treated as bullish.
- Instrument veto: daily return <= -2%, five-session return <= -5%, close below declining SMA50 (compared with its value five sessions before), or falling close plus lower low. Fresh bid more than 2% below completed close also vetoes.
- US equities use SPY and at least 9/11 same-date sector ETFs; UK shares use UK100 and at least 15/21 tracked UK shares; crypto uses BTC and at least 8/12 tracked crypto instruments. These are participation proxies, not full index breadth.
- Regional veto: benchmark daily <= -1.5%, five-session <= -3%, below declining SMA50, or fresh bid <= -1.5% intraday against its last completed close. Basket participation <30% positive with median return < -0.5% also vetoes. A daily sell-off remains blocked until a later completed candle clears those rules; an intraday bounce alone does not override it.
- Mapped US stocks/sector ETFs additionally require same-date sector history and veto sector daily <= -1.5% or close below declining SMA50.
- Supported commodities, FX, bond and precious-metal ETFs receive instrument-only checks, explicitly labeled. Other unvalidated regional/fund mappings wait.
- Thresholds are experimental risk rules, not optimized or proven to improve returns. They may miss profitable rebounds, and passing checks cannot prevent losses.
- Every candidate evaluation is saved in demo_approval_events (latest 5000), with version, timestamps, evidence, status and reason, and included in the bot decision snapshot/logs.
- Dashboard heat map is generated from eToro completed daily bars, with visible dates. It does not scrape third-party heat maps or trade on headlines.

## Research context checked 2 November 2026
Friday 30 October heat maps showed materially different sector outcomes: technology weak while energy and some defensive sectors rose. Thus stock, sector and regional evidence are separate checks. Different providers' weights/prices produce different sector percentages; no external heat-map statistic is hardcoded into the trading engine.
- https://tormere.com/charts/heatmap (dated 30 October close; price-only, constituent-weighted)
- https://tickertiles.com/markets/heatmap/sectors/ (dynamic page; timestamps change)
- https://www.kiplinger.com/investing/stocks/nasdaq-falls-another-747-points-as-intel-is-way-outside-stock-market-today (30 October market report)

81 available tests passed, including actual demo pipeline APPROVED/VETO/WAIT cases, benchmark and intraday crashes, sector/breadth vetoes, UK/crypto separation, missing/stale/misaligned data and incomplete-candle exclusion. Ten unchanged research tests require unavailable local statistical dependencies and are not included. Existing broker/sizing tests isolate approval; the new gate integration test exercises the real approver.
