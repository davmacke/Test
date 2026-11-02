# News and staged entries (2 November 2026)

The news worker is read-only and archives GDELT DOC 2.0 article metadata from an explicit source-domain allowlist. It polls three searches every 15 minutes, paced between requests. This is delayed discovery, not early/exclusive information or a trading wire. No publisher article bodies/images are downloaded. No new paid subscription is created.

Every record retains its provider timestamp and original bot first-seen time. GDELT timestamps are discovery times; the public Dow Jones RSS fallback supplies publication dates with explicit timestamp labels. The fallback is used when no GDELT query returns accepted results, and failures retain their HTTP status where available. Availability for a future point-in-time replay is the later of those times; publication time is unknown for GDELT records. Revised titles are separate records. Empty/failed searches are explicitly unavailable and do not imply calm markets. API coverage is partial, capped at 100 matches per query, and syndicated reports are not independent corroboration. Mention tags include negations/quotations, so are not event verification, directional sentiment or likelihood estimates.

The News & staged entries tab shows sources, timestamps, scenario mentions, coverage and research results. It does not approve/submit orders, average down live positions, alter broker stops or extend the run deadline.

Shell comparison uses existing cached 500 daily candles where available. Equal £1250 budgets fund buy-and-hold; five 20% purchases at least seven calendar days apart; or five maximum conditional purchases after a recovering prior close above SMA20, at a next open below the last purchase. Prior completed data determine recovery, with no future close used for entries. GBX prices become GBP. The 126-session historical windows overlap. Assumed entry/exit frictions are 0.6%/0.1%; dividends, cash interest and financing are excluded, with no research stops. These are retrospective price-based comparisons, not historical news backtests or evidence of future recovery.

Context: renewed escalation and successful diplomacy are competing scenarios. Brent already carried a war premium; an increase in tensions need not produce a surprise price increase. Regular DCA means a fixed recurring investment; discretionary additions following a loss are averaging down and increase exposure.

Sources:
- https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- https://www.investor.gov/introduction-investing/investing-basics/glossary/dollar-cost-averaging
- https://live.euronext.com/en/financial-news/oil-drops-almost-4-hopes-progress-talks-ending-us-iran-war
- https://apnews.com/article/8722c8b5a9deb228e7a188d4760ea8ed

Validation: 90 available tests passed, including source/timestamp/first-seen preservation, changed titles, malicious URLs, unavailable feeds, equal-budget costs/cash and real demo approval regressions. Ten unchanged statistical research tests are excluded because their local dependencies are unavailable.
