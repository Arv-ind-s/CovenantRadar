# Market intelligence: market pressure on covenant cushions

Covenant tests use the last reported quarter. Markets keep moving after that
quarter closes, and the next statements arrive weeks after the following quarter
ends. `/intelligence` fills that gap. For each borrower in your scope it measures
how far official commodity, currency and rate data has moved since the borrower's
last statements. It then sets that move against how much the borrower's
interest cover can absorb.

The screen answers three questions:

1. **What moved?** Driver tiles show each market series that reaches your book,
   its latest value and as-of date, and its change against the reported
   quarter's average.
2. **Where does it land?** Borrowers are ranked by market pressure on interest
   cover. Each card has a plain-language implication for the next two quarterly
   tests.
3. **Why did it move?** Recent reporting from established business and trade
   press is grouped by the market it explains.

## Sources

| Data | Provider (via [FRED](https://fred.stlouisfed.org)) | Series | Frequency |
| --- | --- | --- | --- |
| Brent crude | U.S. Energy Information Administration | `DCOILBRENTEU` | Daily |
| USD/INR | Federal Reserve H.10 | `DEXINUS` | Daily |
| Overnight call rate (tracks the RBI repo rate) | OECD | `IRSTCI01INM156N` | Monthly |
| 10-year G-sec yield | OECD | `INDIRLTLT01STM` | Monthly |
| Iron ore, thermal coal, aluminium, copper, cotton, sugar, wheat, Asia LNG, natural rubber | IMF Primary Commodity Prices | `PIORECRUSDM`, `PCOALAUUSDM`, `PALUMUSDM`, `PCOPPUSDM`, `PCOTTINDUSDM`, `PSUGAISAUSDM`, `PWHEAMTUSDM`, `PNGASJPUSDM`, `PRUBBUSDM` | Monthly, about one month in arrears |

News comes from Google News RSS searches for market terms such as "crude oil
price", "rupee dollar" and "RBI repo rate", limited to the last 14 days. Each
headline keeps its publisher and date. Social reposts and chart widgets are
dropped, and established business and trade press ranks first.

Queries name markets only. **Borrower names and financials never leave the
application.** No API keys are needed.

## Method

All figures come from the borrower's latest complete, non-superseded quarterly
period, together with its tightest live `interest_coverage_ratio` covenant
(EBIT / finance cost). Annual and half-yearly statements are skipped, because
the rate tolerance is sized on one quarter's finance cost.

- **EBIT cushion**: `EBIT − minimum × finance cost`. This is how far quarterly
  EBIT can fall before the covenant breaches. It is a borrower fact.
- **Rate tolerance**: `4 × cushion / minimum / total debt`, in basis points. This
  is the rate rise that breaches the covenant if all debt reprices for a full
  quarter. It is an upper bound, because fixed-rate or hedged debt does not
  reprice.
- **Two lenses on every move**, both measured against the reported quarter's
  average:
  - *Quarter so far* averages everything since the quarter ended. This is what
    the next, unreported quarter is absorbing.
  - *Latest* is the most recent observation. This is what the following quarter
    absorbs if prices hold.
- **Rupee terms.** Dollar-priced commodities are converted with USD/INR over the
  same windows.
- **Input-cost basket.** Each sector maps to the commodities it buys, with an
  assumed share of revenue (`SECTOR_EXPOSURES` in
  `services/market_intelligence.py`). The basket move is the share-weighted
  average, so a fall in steel can offset a rise in rubber.
- **Breakeven**: `cushion / (revenue × basket share)`. This is the basket rise
  that would use the whole cushion if nothing is passed through.
- **Revenue exposures.** For exporters such as IT services, pharma and textiles,
  a weaker rupee is supportive and a stronger rupee is adverse. No breakeven is
  claimed for these.

| Status | Meaning |
| --- | --- |
| Already below minimum | Interest cover is below the minimum on reported numbers |
| Move exceeds cushion | Either lens is beyond the breakeven or rate tolerance |
| Move uses most of cushion | At least 50% of the breakeven |
| Adverse, within cushion | An adverse move below 50% |
| Moves easing | Costs have fallen, or revenue exposures have improved |
| No tracked exposure | No mapped market driver, and rates are flat |
| Market data unavailable | The series have no data for the period; nothing is inferred |

### What this is not

- The cost shares are analyst defaults, not borrower facts. They appear on every
  line they affect and in the method panel. Replace them with the borrower's own
  cost structure where it is known.
- No projected ratio is published. The overlay does not change stored ranks,
  forecasts, bands or covenant results.
- To act on a result, use **Record market review in case**. It drafts a case
  note that cites every series (with FRED links) and headline. The draft is
  capped to fit the 4,000-character case-note limit. Nothing is written until
  the reviewer submits the note.

## Where it appears

- `GET /intelligence` shows the full screen. `?driver=brent` (or any driver key)
  filters to exposed borrowers. HTMX polls every 5 seconds while sources load,
  then at the refresh interval.
- `GET /intelligence/data` returns the same briefing as JSON.
- `POST /intelligence/refresh` forces a refresh, with a 60-second floor per
  source.
- `GET /intelligence/review/{borrower_reference}` prepares the case note and
  requires `UPDATE_CASE`.
- Each portfolio queue row shows a badge (**Market: move exceeds cushion**,
  **Market: move uses most of cushion** and so on). The expanded row shows the
  headline, implication, cushion, rate tolerance and two cited headlines.

Viewing requires `VIEW_QUEUE`. Portfolio scope is enforced by the borrower and
triage repositories. The shared cache holds public data only.

## Fetching and caching

- Requests never wait on the network. When a source is due, one background
  refresh starts, and the page is served from the cache until it finishes.
- Each source fails on its own. After a failure, the last good data stays in
  use, labelled Stale.
- Snapshots are written atomically to disk and survive restarts. Older cache
  formats are ignored.
- If a series has never loaded, its tile says so. **No synthetic or estimated
  value is substituted.**
- FRED's edge stalls requests with a custom User-Agent, so the default httpx
  agent is used.

| Environment variable | Default |
| --- | --- |
| `COVENANT_RADAR_INTELLIGENCE__ENABLED` | `true` |
| `COVENANT_RADAR_INTELLIGENCE__CACHE_PATH` | `var/market-intelligence.json` |
| `COVENANT_RADAR_INTELLIGENCE__REFRESH_SECONDS` | `900` |

`python scripts/demo_up.py --without-ml --port 8001` starts a demo with live
data. Add `--offline-data` to disable public requests. The cache lock is
process-local, so a multi-worker deployment should move fetching to a shared
worker.

## Tests

```sh
python -m pytest tests/unit/test_market_intelligence.py tests/unit/test_borrower_market.py \
  tests/integration/test_market_intelligence_routes.py tests/integration/test_queue_market_impact.py -q
```

The tests replay real FRED and Google News responses captured on 29 Sep 2026
(`tests/fixtures/market/`), so they run offline. They cover:

- parsing, publisher filtering, unsafe XML and link rejection, and redirect
  limits
- lens and rupee-conversion arithmetic, basket netting, breakeven and rate
  tolerance
- failure isolation, stale fallback, restart, throttling and background refresh
- scope isolation, HTML escaping, permissions, and the review draft's citations
- a check that stored queue ranks and scores are never changed
