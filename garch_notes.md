# GARCH(1,1) volatility diagnostics

Version garch11_v2, 2 November 2026. This replaces the coarse finite-grid aggregate helper with a common variance-targeted engine and adds independent per-instrument forecasts for held assets, benchmarks and current strategy candidates. Per-instrument coverage remains useful when aggregate portfolio covariance coverage is insufficient.

## Estimation and units
- Simple native-currency close-to-close returns in decimal units. Multiplying all prices by 100 (GBP to GBX) leaves volatility unchanged. Per-instrument estimates do not include separate currency-conversion risk.
- At least126 returns, up to252 for the current fit. Gaussian quasi-likelihood with variance targeting: omega = sample variance * (1-alpha-beta). Grid initialization plus bounded local coordinate refinement, alpha/beta>=0 and alpha+beta<0.995. This is not an unconstrained full maximum-likelihood fit; initialization uses training sample variance.
- Missing, nonfinite, extreme return-scale or degenerate variance inputs return explicit statuses. Near-boundary estimates are flagged; persistence and half-life may be weakly identified.
- Recursion: sigma²(next) = omega + alpha*innovation² + beta*sigma²(current). Long-run variance omega/(1-alpha-beta); variance-shock half-life log(0.5)/log(alpha+beta).
- Point variance at horizon h is long-run variance + persistence^(h-1)*(next variance - long-run variance). Cumulative variance sums the point conditional variances. Forecast horizons are observations/sessions, not guaranteed calendar dates.
- The hypothetical shock is an innovation inserted into the next observation; projections then describe sessions after it. Both signs have the same response in symmetric GARCH. A stress scenario is not an event probability or a directional prediction.

## Historical check
When189+ returns exist, the final63 are withheld from parameter estimation. The training GARCH parameters and mean remain fixed; variance updates sequentially after each held-out observation. Compare average QLIKE with EWMA(0.94) and constant training variance. Lower is better. A single historical split is not robust evidence of future forecasting skill. The current model is then fitted using the latest252 returns.

## Runtime
The existing separate hourly read-only growth worker caches500 completed daily bars for holdings, benchmarks and currently signaled candidates. It records the model version, last completed candle, forecast origin and data limitations. Version migration triggers one immediate evaluation without resetting the account baseline or changing the Friday6November21:00UTC execution deadline. The Volatility (GARCH) tab shows coverage, projections, a shock slider and held-out checks. GARCH_RESULT logs bounded focus diagnostics for Shell, BP, XLE and SPY.

No order, stop, target or sizing changes. Completed daily data exclude today's incomplete move. Volatility estimates do not forecast war, gap timing or price direction. Symmetric GARCH misses leverage asymmetry; normal-tail numbers retained in the aggregate diagnostic are approximations, not dependable loss bounds. Corporate actions and adjusted-price quality are not independently verified.

Validation:95 available tests cover the photo's recursion, positive/negative shock symmetry, multi-session decay, stationary positivity, price/return scaling, held-out leakage prevention, incomplete/stale histories, degenerate portfolios and full report integration. Ten unchanged statistical research tests are excluded due to unavailable local dependencies.

Primary method references:
- https://arch.readthedocs.io/en/stable/univariate/forecasting.html
- https://mc-stan.org/docs/stan-users-guide/time-series.html
