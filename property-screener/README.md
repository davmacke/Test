# Property Scout

A small England buy-to-let research dashboard, separate from the eToro analyser. It saves listings, prices, evidence notes, scan failures and change history in SQLite. It does not contact agents, place offers or bid at auction.

## The user's screen

- England; 2–3 bedrooms.
- Up to £100,000 for ready/minor-work houses; £70,000 for refurbishment projects.
- £25,000 for deposit and works; purchase fees, stamp duty and any buyer premium are separate.
- Base illustration: 75% LTV, 5% interest-only; stress illustration: 7%.
- Listed buildings excluded. Unknown listed status is a missing check, never treated as unlisted.
- Works contingency, initial mortgage interest and assumed council-tax/utilities holding costs are included in capital needed. Rental operating reserves are separate.

At £100,000 the entire pot is the 25% deposit. £2,000 works plus 10% contingency limits the purchase to £91,200 before a reserve. At £70,000, £5,500 works + £550 contingency + three months of model holding costs needs £24,656.25. These works amounts are unquoted caps/allowances, not estimates of a particular house's actual condition.

`model_fits` means the illustration is within the capital pot and has positive pre-tax surplus. `verified` additionally needs supported rent, costs, works, mortgage, title/lease, availability, exact England location and unlisted status; fresh data are required. Caps are affordability limits, not market valuations. Missing rent or lease charges do not become zero. The data extraction is deterministic and intentionally conservative; a human must review it.

## Run locally

Python 3.11+; standard library only:

```
python -m unittest discover -s tests -v
python app.py
```

Open http://127.0.0.1:8765. Eight research snapshots dated 2 November 2026 are imported once. They remain unverified and retain their original observation dates until a successful permitted page fetch. The web app binds to loopback without a password. For any public interface, set `SCOUT_PASSWORD`; username is `owner`. A missing password fails closed on public binding.

For a bounded command-line scan:

```
python app.py --scan-once
```

`AUTO_SCAN=true` starts a durable interval check in the single web process. Default interval is 12 hours, minimum one hour. It checks SQLite's last run, so restarts do not trigger a request burst. Interrupted scans are recorded. Only one scan can run in this process at once. Run one process/instance; SQLite plus a Render disk is intentionally a single-instance design. Use `PAGES_PER_SOURCE` (1–30) to bound listing checks, in addition to at most five discovery pages per source.

## Actual source coverage

Enabled starter adapters: **JW Wood** (North East England sitemap) and **Higson Homes** (Hull listing index). A source is still subject to live robots rules, successful HTML retrieval and parser coverage. The app does not promise whole-market coverage or that an advertised house is still available.

Reeds Rains is disabled because its robots file restricts GPTBot property access. Rightmove, Zoopla and OnTheMarket are links/imported research only; this project does not scrape those portals. No CAPTCHA bypass, residential proxy, logged-in session, paid data service or concealed crawler identity is included. To expand coverage, add estate-agent public feeds/indexes that permit access in `sources.json`, or integrate an authorised data feed. Do not enable a source contrary to its access rules or terms.

The collector checks robots before pages, honours relevant crawl delays, rejects private-network URLs, checks redirect destinations and keeps requests bounded. HTTP failures and blocks never advance an old listing's observation date and never mark a listing sold merely because it disappeared from an index. Failed or changed prices invalidate affected confirmations. Changes are deduplicated by canonical URL; cross-agent duplication requires address review because exact flat/house numbers are often absent.

Automated extraction is unverified: it does not infer market rent from a yield, assume a modern house is unlisted, or assume that a tenanted house meets a mortgage lender's condition requirements. Refurbishment may require a property already in lettable condition for a standard BTL mortgage. Bridging/refinancing and any future value uplift are not assumed.

## Render deployment

`render.yaml` defines one Python web service with a 1 GB persistent disk at `/var/data`, Frankfurt region, `0.5c-512mb` compute and a required password entered as a Render secret. Automatic deploys are off. Automatic scans are on only in this deployment configuration.

This is a **paid deployment proposal**, not permission to incur an unquoted charge. Confirm the current compute and disk price in the Render Dashboard before applying. A free sleeping web service is unsuitable for reliable scheduled scans and durable SQLite history. A cron service cannot share this web service's disk; a separate cron would need a network database.

Render needs the project pushed to GitHub/GitLab/Bitbucket. If using a subfolder of a repository, set root directory to `property-screener` in the service configuration or the repository-root Blueprint. Do not apply this subfolder's Blueprint from a repository root without adjusting `rootDir`/blueprint path. The stock analyser's service, branch and secrets must remain separate.

After deploy: configure `SCOUT_PASSWORD`, verify `/healthz`, sign in as owner, check source health, confirm that a completed scan has persisted after a restart, and inspect sample extracted prices/rents against agent pages before relying on them. The dashboard is private; its health endpoint contains no listing or financial data.

## Data and backup

`DATA_DIR` defaults to `./data`. The database and password are git-ignored. Export/backup SQLite using its backup API while the app is running (do not copy just the database and omit the WAL). Keep snapshots off the service before deleting a disk. The deployed filesystem is ephemeral unless the persistent disk is attached and `DATA_DIR` points into its mount.
