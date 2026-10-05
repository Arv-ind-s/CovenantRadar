# Covenant Radar

**Evidence-backed covenant monitoring and early warning for commercial-credit teams.**

Covenant Radar watches a lending book continuously, predicts which borrowers
will breach a covenant in the next 30, 60 or 90 days, explains exactly why,
and turns that into a ranked list of what a credit desk should do today —
with a stored record behind every number on screen.

- **Domain:** Commercial banking · credit risk · Indian lending conventions
- **Shape:** A server-rendered web workspace, a REST API, a nightly batch
  pipeline and a command-line operations tool — all in one Python application
- **Status:** Version 0.1.0. The demo book is 24 NSE-listed Indian companies,
  tested on the quarterly results they filed with the exchange, with a live
  public-data workspace for official news and economic context. The scoring
  workflow also runs offline.

---

## 1. The problem it solves

A loan covenant is a promise in the contract: *keep leverage under 3.0x*,
*keep interest cover above 1.5x*. Banks usually check those promises when the
borrower files a statement — once a quarter, sometimes once a year.

That leaves two gaps:

1. **The blind window.** A borrower can deteriorate badly between two filing
   dates. By the time the next statement arrives, the breach has already
   happened and the bank's options have narrowed.
2. **The unread signals.** The warning signs were usually already visible —
   the overdraft ran hot, payments slipped, cash outflows changed shape, the
   industry turned — but they sat in different systems and nobody joined them
   to the covenant that was actually at risk.

The obvious fix — "add an AI model that scores risk" — creates a third
problem. A credit officer cannot act on a score they cannot defend. A waiver
decision, an exposure reduction, an escalation to the risk committee: each one
has to be justified to a colleague, a committee, an auditor and a regulator.
An opaque number is not usable evidence.

**Covenant Radar closes the blind window without creating that third problem.**
It keeps the contractual covenant maths deterministic and inspectable, adds
behavioural and external signals as a separate, clearly-labelled layer, and
makes every warning reconstructable from stored records — the source data, the
threshold in force at the time, the calculation, the trend, the evidence, and
who did what next.

---

## 2. What it does, end to end

Six things happen, in order, every night. Each one is a real, independently
retryable job (`nightly.ingest` → `nightly.test` → `nightly.score` →
`nightly.rank` → `nightly.update_cases` → `nightly.dispatch`).

### Step 1 — Ingest and normalize

Borrower financial statements arrive as CSV, XLSX or PDF. Every bank labels
its lines differently, and Indian statements are inconsistent about sign
conventions. The importer maps whatever arrived onto **one normalized chart of
accounts** in ₹ crore, checks the balance-sheet and P&L identities actually
add up, and records where each figure came from.

Rows that don't pass go to a **quarantine queue** for a data steward to fix —
they are never silently dropped, and never silently guessed.

Behavioural and external signals arrive on the same tick, across seven closed
families:

| Family | What it measures | Unit |
|---|---|---|
| `account_activity` | Change in account activity | % |
| `payment` | Days past due | days |
| `utilisation` | Facility / limit utilisation | % |
| `treasury` | Cash-outflow ratio | ratio |
| `concentration` | Top-group exposure share | % |
| `industry` | Industry stress score | score |
| `news` | News risk score | score |

Signal events are **immutable and content-addressed**: two different feeds
describing the same event produce the same identity, so re-running an import
cannot double-count.

### Step 2 — Test the covenants (deterministic, no model)

For each live covenant, the engine computes the named ratio from the
normalized statement and compares it to the threshold in force **on that
date** — honouring approved exceptions (a threshold changed for a range of
periods), waivers (effective only after approval, for a date range), and cure
periods (a failing test that a later passing retest inside the window
resolves; a breach that continues keeps the window its first breach opened,
and becomes a plain breach once that window has passed).

A statement covenant is tested **once per statement period**, under that
period's label, and again only when a retest is queued (a restatement, a
waiver, an exception). Covenants that read daily facility conduct
(utilisation, drawing-power headroom) are tested daily. When the next
statement is overdue — a reporting period plus the filing grace
(`forecast.statement_grace_days`, 60 days) after the last period end — the
covenant is marked **stale** once.

The library ships **24 covenant definitions**, each with its own formula,
required statement lines, unit and plausible band:

> leverage · DSCR · interest cover · fixed-charge cover · current ratio ·
> quick ratio · TOL/TNW · debt/EBITDA · net debt/EBITDA · EBITDA margin ·
> TNW floor · minimum net worth · utilisation · drawing-power headroom ·
> receivable days · inventory days · payable days · cash conversion cycle ·
> working-capital gap · asset cover · minimum liquidity · maximum capex ·
> dividend restriction · promoter-shareholding floor

A bank can also register a **custom formula**. The text is parsed with
Python's own parser and walked against a closed allow-list (statement-line
names, `+ - * /`, brackets). It is never handed to `eval`, so a typed formula
can never become code execution.

If a ratio cannot be computed, the result says so with an **enumerated
reason** — missing line, zero denominator, outside plausible band — never a
free-text sentence that two auditors could read two ways.

The engine also derives the **supervisory SMA band** from facility conduct.

### Step 3 — Score the evidence and forecast the crossing

This is where the early warning happens. Three separate things are computed,
and each is stored:

**The evidence ledger.** Raw signal events are turned into evidence *items*.
An item has a stable identity — `(borrower, facility, family, type)` — so a
payment delay that grows from 5 days to 15 days is *one evolving item*, not
two. Items are scored on:

- **Persistence (T3)** — is this sustained or is it noise? Two independent
  arms: a consecutive-day run, and an event count inside a rolling window.
- **Materiality (T4)** — would this actually erode covenant headroom? Each
  affected covenant is evaluated separately; the largest projected 90-day
  headroom erosion becomes the item's score. Improvement scores zero.
- **Decay** — older observations contribute less pressure (T3 `decay_rate`,
  0.95 a day from the last adverse observation), but *remain visible in the
  ledger*. Weighting and retention are deliberately different things.
  Persistence is measured inside the current window, and a healthy
  observation after the last adverse one retires the warning.
- **Supersession** — when later evidence contradicts an earlier reading, a
  new item is written and the old one is marked superseded. Nothing is
  deleted or edited.

**The forecast path.** A least-squares trend is fitted over the usable
observations, each dated at the period end it describes. Day zero is the
latest real value; the fitted per-day drift is one term; the directional
pressure from sustained evidence is the other. Evidence materiality is
dimensionless, so it is converted into the covenant's own units: sustained
evidence at full materiality moves a covenant by `forecast.pressure_rate`
(25%) of its threshold over one reporting period. That gives a **daily
projected value for 90 days**, stored as a path — not drawn on the fly in the
browser.

**The crossing and the probability.** The path is walked for the first
inclusive crossing of the contractual boundary, and that day offset is turned
into a calendar date. Separately, three saturated signals — **distance** to
the boundary, **velocity** of change, **pressure** from sustained evidence —
are combined with configured weights (0.50 / 0.30 / 0.20, capped at 0.99) into
a breach probability. The inputs are dimensionless, so a 3.0x leverage limit
and an 85% utilisation cap are judged alike, and each is credited only for
its own effect: distance is today's cushion as a fraction of the threshold
(stretched by `forecast.distance_scale`, 2, so a flat covenant is amber on
distance alone only inside about 12% headroom), velocity is the share of that
cushion the financial trend uses up over the horizon, and pressure is the share
sustained warning evidence uses up. "Today" is the trend carried forward from
the last statement's period end to the scoring date, so the weeks since the
last filing are not a blind spot. A projected crossing inside the horizon fixes
the probability at the cap; a covenant already in breach on its latest test
stays at the cap even when its statement is overdue. The
intervention simulator reads the same inputs, so its do-nothing baseline is
the stored forecast.

**Confidence is separate from probability, and can suppress it.** A confidence
product is computed from data completeness and related factors, including
staleness — the days the data is overdue past its expected reporting lag, so a
current quarterly statement is not penalised for being a quarter old. Below
the floor (T2), the number is *not shown at all* — the screen says the view is
unsupported rather than displaying a figure the data cannot carry.

**Driver attribution.** Every contribution is stored signed. Drivers at or
above the T5 share are listed individually; smaller positive ones are folded
into "other"; **negative (risk-reducing) drivers are always kept separate**,
because hiding an improving factor would misrepresent the explanation. When a
projected crossing fixes the probability at the cap, the lift above the raw
score is credited to whatever moved the path across the limit — the trend and
each sustained evidence item, in proportion to how far each moved it.

**A shadow ML challenger.** A local scikit-learn model (calibrated logistic
regression and gradient boosting, one per horizon) runs alongside the
deterministic forecast. Its artifact is checksum-verified and loaded from
disk; it never makes a network call and never receives an identifier. The cross-platform demo launcher trains and selects the artifact, then runs it
in **`shadow` mode** (ML is disabled in the base config until an artifact is configured): its prediction is recorded
on every forecast, but the queue, the band and the case are still built from
the deterministic probability. Promoting it to champion requires an approved
model registration — it cannot be switched on by editing a config file alone.

### Step 4 — Rank the portfolio

Stage 6 reads the persisted forecast facts. It never calls a model, never
recomputes a forecast, and **never silently drops a borrower**.

Urgency combines probability, exposure and confidence. Borrowers are banded
against T1 (`Act` ≥ 0.70, `Amber` ≥ 0.40, otherwise `Watch`) and ordered band
first — every act borrower before any amber one — then by urgency within the
band, so a large amber exposure never sits above a borrower already in act. A forecast below
the T2 confidence floor is kept as a **suppressed watch entry** — visible, but
its probability is not used to manufacture urgency. A borrower with no usable
forecast still appears, with an explicit state and reason, after the rankable
rows.

A separate comparison against the previous run produces the **"what changed"**
column — including borrowers that dropped out of monitoring entirely.

### Step 5 — Open and update cases

A case is the record of work done against a warning. It has an explicit
lifecycle state machine (states can only move through the defined table — no
caller can assign an arbitrary string), an owner, and an **SLA derived from
T11** (24h for Act, 72h for Amber, 168h for Watch). Closing is terminal:
re-escalation creates a new case linked to the closed one.

### Step 6 — Dispatch notifications

Notifications resolve real recipients, apply portfolio and permission checks
to **every scope-bearing value** before it leaves the system, honour user
preferences, and only then write a durable queued row. Channels: in-app,
email, bundled email digest, and signed webhook. A channel adapter receives a
fully-rendered message — it never loads records and never makes an
authorization decision.

---

## 3. The differentiators

These are the design choices that make the product defensible in a credit
committee, not just demonstrable in a browser.

### The contractual covenant is kept separate from the early-warning signal

The covenant test is deterministic arithmetic on filed statements, with the
threshold in force at the time. The behavioural signals are a *different
layer* that produces pressure and drivers. They are never mixed into one
opaque score, so an officer can always answer "is this a breach, or is this a
warning?"

### Seven stages, and only two of them use a language model

Every decision the product makes belongs to one of seven numbered stages, and
each stage records **who decided** — `code`, `statistical`, or `model`:

| Stage | Name | Decider |
|---|---|---|
| 1 | Intake | model — proposes, decides nothing |
| 2 | Covenant engine | code |
| 3 | Evidence ledger | code |
| 4 | Forecast | code (+ statistical challenger in shadow) |
| 5 | Intervention | code |
| 6 | Triage | code |
| 7 | Memo | model — drafts prose from stored slots |

A model never computes a covenant, a probability, a crossing date, a ranking
or a credit decision. It reads a clause and proposes fields; it writes prose
from figures that were already persisted. That boundary is enforced by code,
not by convention.

### Six independent checks disprove every model proposal

When the model reads a sanction letter and proposes "leverage ratio, max,
3.00x, quarterly", **all six checks always run** — never short-circuited at
the first failure, so a reviewer sees every reason at once:

1. the reply matched its declared output shape exactly;
2. the definition is a known library entry **or** a valid custom formula —
   not both, not neither;
3. **the ratio is actually recomputable from this borrower's own filed
   statements** — a proposal can name a real ratio and still fail here,
   because the statement this borrower filed is missing the line it needs;
4. the threshold falls inside the definition's plausible band;
5. the unit and currency agree with the definition and the facility;
6. the frequency is unambiguous and the effective date is consistent with the
   facility's sanction date.

A failed check is struck through and **cannot render a confirm control**. The
model's clause text is also checked for prompt-injection shape before it is
combined with the report.

### False positives are controlled by design, and the calibration is recorded

Persistence (T3), materiality (T4) and the confidence floor (T2) exist
specifically to stop transient noise becoming an escalation. The shipped
threshold values are not guesses — they come from a recorded calibration run
against the labelled reference portfolio, kept in
[`docs/calibration/reference-portfolio.md`](docs/calibration/reference-portfolio.md)
with the rejected alternatives included:

| Check | Requirement | Result on the reference build |
|---|---|---|
| G1 — advance warning | ≥70% of deteriorating borrowers warned ≥30 days ahead; ≥50% ≥60 days ahead | 100% / 100% |
| G3 — escalation restraint | ≤10% of the book escalated on any day; ≤5% false escalation | 8.33% / 0% |

A rejected trial is recorded too: loosening T3 from three events to one raised
the daily escalation share to 16.67% and false escalation to 50%, so it was
refused and the original setting stayed. These are synthetic-portfolio
results, and the document says so explicitly — a customer must repeat the
procedure on their own history.

### Every number resolves to a stored record

The **why-panel** (`/why/{subject_type}/{subject_id}`) walks any figure back
through its stage trace and shows: what the stage received, what it produced,
which thresholds were compared, the rule or model version, and the source
records. **Warning reconstruction** rebuilds an entire past warning using
point-in-time reads, so a later change can never alter what a past
reconstruction shows.

An **evidence bundle** exports the whole thing as an ordinary ZIP: the
reconstruction, a human-readable rendering, the relevant audit-chain rows, and
each source document (or an explicit record of why it could not be included).
The verifier has **no dependency on the database, encryption keys or a model
provider** — an outside party can check the bundle independently.

The audit table itself is a **hash chain** with canonical JSON encoding, so
tampering is detectable.

### Recommendations stay advisory, and simulation stays bank-owned

The browser cannot supply an effect model. It can only pick from the bank's
own catalogue of interventions, each with one of five closed effect types
(`level_shift`, `rate_change`, `threshold_relaxation`, `pressure_reduction`,
`combination`), declared parameters,
declared assumptions, the covenant classes it applies to, and whether it needs
approval. Four active entries are seeded:

| Code | For | Applies to | Effect | Approval |
|---|---|---|---|---|
| `RM-REVIEW-CONDUCT` | Relationship manager | conduct, liquidity | Reduce conduct pressure 25% | No |
| `CREDIT-REPAY-TERM-DEBT` | Credit | leverage | Term-debt prepayment: level −0.60 | Yes |
| `CREDIT-REDUCE-DEBT-SERVICE` | Credit | coverage, liquidity | Exposure and debt-service reduction: level +0.60 | Yes |
| `RISK-REVIEW-THRESHOLD` | Risk | leverage, coverage, liquidity, net_worth | Threshold review: relax 0.10 | Yes |

A `level_shift` amount is signed in the ratio's own units, so one entry is
correct for exactly one covenant direction: −0.60 improves a `max` covenant
such as leverage and worsens a `min` covenant such as coverage. That is why
the credit action is two entries rather than one. The superseded
`CREDIT-REDUCE-EXPOSURE`, which declared a single −0.10 shift across both
directions, is retired rather than edited: a persisted simulation's content
hash covers the catalogue amount, assumptions and applicable classes, so
changing them in place would make every simulation already stored against
that code fail its idempotency check.

The simulator changes **one named input** and re-runs the *same* forecast,
crossing and probability functions the nightly pipeline used, then shows the
counterfactual beside a **"do nothing" baseline** — with the assumptions
printed under each option. Nothing is mutated: the stored forecast is
untouched and every simulation is persisted.

### The risk view can be revised without destroying history

A human **override** is a new append-only fact. It snapshots what was
displayed, stores the replacement value and the reason, and derives the
current view by applying the latest replacement over the original. The
forecast row is never edited. **Dispositions** work the same way: a later
answer from the desk is another row, so the sequence of decisions after a
warning is fully reconstructable.

### Built for Indian commercial credit specifically

Figures in ₹ lakh/crore with Indian digit grouping; IST-aware dates and the
T12 nightly deadline; fiscal-year testing calendars with holiday and weekend
adjustment; CIN/PAN handled as personal-class data; supervisory SMA banding;
CRILC monthly extract and weekly default report for exposure at or above ₹5
crore; an EWS/RFA committee pack that supplies the evidence trail and
deliberately carries a "no fraud determination" notice, because that
classification is a human regulated act.

### It runs completely offline

Recorded model responses ("cassettes") are a real provider adapter, not a test
helper — same interface as a live provider, selected by configuration. They
**never fall through to the network**. Combined with the committed snapshot of
the demo companies' filed results, the full scoring, alerting and demo workflow
runs air-gapped on a laptop.

The market-intelligence workspace uses public endpoints when enabled. Set
`COVENANT_RADAR_INTELLIGENCE__ENABLED=false` (or launch the Python demo with
`--offline-data`) for an entirely offline session. Cached public records remain
visible with an offline label.

### Real events become reviewable work

Covenant tests use the last reported quarter, but markets keep moving. The
**Market intelligence** workspace (`/intelligence`) measures how far each
borrower's sector drivers have moved since its last statements. The drivers are
Brent, USD/INR, the call rate, the 10-year G-sec yield, and IMF prices for iron
ore, coal, aluminium, copper, cotton, sugar, wheat, LNG and rubber, all from
official series via FRED. Each move is set against the borrower's own
interest-cover cushion, meaning how far EBIT can fall, or rates can rise, before
the covenant breaches.

Every figure carries its source and as-of date. Two lenses separate the
unreported quarter so far from what happens if latest prices hold. For example:
"Brent +13.4% at 22 Sep 2026 prices vs a 1.1% breakeven. The Sep quarter test
should benefit from lower average costs so far, but if prices hold, the Dec
quarter test is at risk." Recent reporting from Google News, grouped by market,
explains each move. Breakevens use sector cost-share assumptions, which are
shown on every line. **Record market review in case** drafts a cited case note.

The **portfolio queue** shows a market-pressure badge and an expandable panel
per borrower. Neither changes the stored ranking, probabilities, bands or
covenant results.

See [the live-data guide](docs/market-intelligence.md) for the method, sources,
configuration and limitations.

---

## 4. Feature catalogue

### Screens

| Screen | Route | What it does |
|---|---|---|
| Portfolio queue | `/` | Ranked action list with band, portfolio, industry, assignee, SMA-band and case-state filters; summary strip; mini-trajectories; "what changed"; saved views; bulk actions |
| Market intelligence | `/intelligence` | Commodity, rupee and rate moves since the last statements set against each borrower's interest-cover cushion; driver tiles, cited news, source health and a cited case-note workflow |
| Borrower case file | `/borrowers/{ref}` | Header facts, covenant position, forecast trajectory with a day selector, evidence margin, signals, documents, memo block and case actions |
| Forecast trajectory | (in case file) | The stored daily path with threshold marker, crossing annotation, named 30/60/90 stops, and per-day value, headroom, probability, confidence and drivers |
| Why this decision | `/why/{type}/{id}` | Per-stage explanation: inputs, outputs, thresholds compared, decider, rule/model version, source records |
| Intervention simulator | `/simulator` | Compare up to four catalogue interventions against a do-nothing baseline; carry results into a memo |
| Covenant intake | `/intake` | Upload a sanction letter, side-by-side source/proposal view, six verification verdicts, confirm-to-register |
| Covenants | `/covenants`, `/covenants/approvals` | Register, amend, view versions; maker-checker approval queue |
| Cases | `/cases`, `/cases/{ref}` | Ownership, lifecycle, SLA, append-only history |
| Certificates | `/certificates` | Compliance-certificate requests derived from the testing calendar |
| Statements | `/financial-statements`, `/statements/quarantine`, `/statements/restate` | Import a statement, correct or reject quarantined rows, restate a prior period |
| Master data | `/borrowers`, `/facilities`, `/portfolios` | Borrowers, facilities (with an insights view), portfolios; effective dating and optimistic concurrency |
| Search | `/search` | Scope-safe global search; personal-data hits are audited |
| Audit | `/audit` | Audit search, export, reconstruction view, bundle status |
| Governance | `/governance` | Threshold proposals and approvals, model register, evaluation results |
| Notifications | `/notifications` | In-app inbox |
| Administration | `/admin/users`, `/admin/jobs`, `/admin/config`, `/admin/catalogue` | Users and roles, job runs and retention, configuration, intervention catalogue |
| Documents | `/documents/review` | Document viewer with source spans |
| Exports | `/exports/{id}` | Asynchronous export status and download |
| Auth | `/sign-in`, `/mfa/*`, `/password/change`, `/sso/*` | Password, TOTP MFA, OIDC and SAML SSO |

### REST API (`/api/v1`)

Read-heavy and versioned. Borrowers, portfolios, facilities, covenants,
covenant tests, evidence, forecasts, cases, memos, simulations, audit events,
`GET /explain/{subject_type}/{subject_id}`, and two write endpoints —
`POST /ingest/signals` and `POST /ingest/statements`.

- **Two credential kinds, interchangeable on every route:** the signed browser
  session cookie, or a scoped API key (`Authorization: Bearer …`). A key
  carries its own permission scopes, portfolio scope and per-minute rate
  limit. It is shown once at issue; only a prefix and a SHA-256 digest persist.
- **Out of scope returns `404`, never `403`** — scope is never an enumeration
  oracle.
- **Opaque HMAC-signed cursor pagination.** A cursor from a different filter
  set is refused with `422`, not silently reinterpreted.
- **`If-None-Match` / `304`** on detail resources.
- **One error envelope** for every failure:
  `{"error", "message", "field", "request_id"}`.
- **The OpenAPI document is generated from the live route table**, never
  committed. A contract test checks it in both directions on every commit, so
  it cannot drift from the implementation.

### Operations

- **Nightly pipeline** with six retryable steps sharing one run id.
  Idempotent per run id; a halted run leaves later steps untouched so the
  prior day's results keep serving the queue.
- **Per-item savepoint isolation** — one bad borrower is recorded and skipped,
  never halts the book, and stays visible in that step's metrics.
- **Deadline (T12) and recurring-failure escalation** raised as durable
  records from the job ledger's own history.
- **Startup self-checks** on every process: configuration, migrations at head,
  database, document store, scheduler registry, clock skew. A database
  circuit-breaker serves an immediate maintenance response instead of queuing
  behind timeouts.
- **Integrity check** — a read-only scheduled job verifying the audit chain,
  referential integrity, threshold-snapshot references and document-store
  consistency, reported as job metrics so "did the check even run" is
  answerable forever.
- **Observability** — `/health`, `/ready`, `/version`, `/metrics`
  (Prometheus), structured logs with redaction, OpenTelemetry tracing, and
  retention policy.
- **Exports** — CSV and XLSX list exports, asynchronous and job-backed;
  memo exports to PDF and DOCX with a stable integrity digest; a labelled
  feedback corpus export that is an explicit allow-list, so legal names,
  CIN/PAN and free text can never cross the boundary.

---

## 5. Who uses it, and how

Eight roles, enforced server-side, with 33 distinct permissions. These are not
cosmetic: `admin` holds neither `RUN_INTAKE` nor `RUN_SIMULATION` and genuinely
cannot open those screens, and `credit` cannot approve its own registration.

| Role | Login | Typical day |
|---|---|---|
| Risk Head | `riskhead` | Opens the queue, works the Act band, runs simulations, approves threshold changes and model promotions |
| Risk Officer | `risk` | Portfolio-wide queue, simulator, memos, overrides |
| Credit Officer | `credit` | Uploads sanction letters, runs intake, registers covenants |
| Credit Approver | `approver` | Approves what `credit` registers — the maker-checker pair |
| Relationship Manager | `rm` | Assigned portfolio only; case files, agrees actions, exports memos |
| Auditor / Inspector | `auditor` | Read-only everywhere, including the audit trail and evidence bundles |
| Administrator | `admin` | Users, jobs, connectors, configuration |
| Data Steward | `steward` | Quarantine review and source-data corrections |

### The main workflow: morning triage

1. **Open the queue.** It is already backed by the completed overnight run —
   an action surface, not a blank dashboard. If no run completed, the screen
   *says so* rather than fabricating a ranking.
2. **Read the summary strip** — Act now, Amber, Watch, Changed today,
   portfolio exposure.
3. **Open the top case file.** Covenant position with current value,
   threshold, headroom and verdict.
4. **Read the forecast trajectory.** Move the day selector; every selected day
   shows projected value, headroom, probability, confidence, crossing date and
   drivers — all from the stored path.
5. **Open "Why this decision"** on the figure being questioned. See the rule,
   the observed value, the threshold, the comparison side and the source
   record.
6. **Compare interventions** in the simulator against doing nothing.
7. **Generate a memo** grounded in that same forecast, evidence and set of
   simulations — drafted prose marked as drafted, figures traceable, advisory
   statement that human credit review is required.
8. **Record the decision** as a case update or disposition, or override the
   risk view with a reason. All append-only.

### The second workflow: covenant intake

Upload a sanction letter → deterministic clause detection narrows a
forty-page document to candidate lines → the model reads **one masked clause
at a time** and proposes fields → six code checks try to disprove it → the
reviewer sees source and proposal side by side with every verdict → a fully
passing proposal can be confirmed → registration goes through maker-checker
approval before the covenant is live.

---

## 6. Architecture

### Layering, enforced by tooling

The layer rules are not documentation — they are **import-linter contracts
that fail the build**:

```
domain/     Pure business rules. No SQLAlchemy, FastAPI, HTTP, numpy,
            pandas, pydantic — and no other covenant_radar package.
              ↑
services/   Use cases and transaction boundaries. Talks through ports,
            never to adapters (ai, db, web, ingestion, …) directly.
              ↑
db/ ai/ documents/ ingestion/ notifications/ scheduler/ security/
            Adapters. Only ai/client.py may import a model provider.
            Only audit/record.py may write audit rows.
            SQLAlchemy sessions stay inside db/.
              ↑
web/ api/   Presentation. Uses services, never domain internals or db.
```

Because the domain layer is pure, every risk rule can be computed and checked
**before** anything is written, and a scoring run can be replayed from facts
alone.

### The composition root

`create_production_app` is the single boundary that wires everything: one
request-scoped SQLAlchemy session and transaction per HTTP request, signed
HttpOnly browser sessions, RBAC, the audit recorder, the document adapter, the
AI provider and the nightly runtime. Feature routes receive services through
explicit factories — **no screen opens its own database connection**.

It refuses to start without a session signing secret. A browser UI that
silently fell back to an unsigned development identity would break the access
boundary, so this fails loudly instead.

### Explainability is a first-class table

Every stage writes a validated **trace** — a domain value object that knows
nothing about SQLAlchemy, so it can be built and checked before a write is
attempted. Decimals, UUIDs and dates cross the JSON boundary as text; anything
else is converted and *recorded as having been coerced*, so an explainability
row is never a pretence that an arbitrary object serialised faithfully.

### The UI

Server-rendered Jinja templates with progressive enhancement through HTMX and
small vanilla JavaScript controllers — no SPA framework, no build step. The
design system lives in `web/static/css` with self-hosted fonts, light/dark
themes, responsive layouts, keyboard focus states, reduced-motion support and
designed empty, loading and error states. Every interaction has a working
no-JavaScript path (for example, the forecast's named 30/60/90 horizon stops
beside the range input).

---

## 7. Technology stack

| Layer | Choice | Version |
|---|---|---|
| Language | Python | 3.12+ |
| Web framework | FastAPI | 0.141.1 |
| ASGI server | Uvicorn | 0.52.4 |
| Templating | Jinja2 | 3.1.6 |
| Front end | HTMX (vendored, self-hosted) + vanilla JS | — |
| ORM | SQLAlchemy | 2.0 |
| Migrations | Alembic | 1.14 |
| Database | SQLite (demo) / PostgreSQL via psycopg | 3.2 |
| Validation & settings | Pydantic + pydantic-settings | 2.11 / 2.7 |
| Scheduling | APScheduler | 3.11 |
| ML | scikit-learn, numpy, pandas | 1.6 / 2.2 / 3.0 |
| Crypto & auth | cryptography, argon2-cffi, itsdangerous, authlib, pysaml2 | — |
| Documents in | pdfplumber, pypdf, openpyxl, ocrmypdf | — |
| Documents out | WeasyPrint (PDF), python-docx (DOCX) | 63 / 1.1 |
| Observability | structlog, prometheus-client, opentelemetry-sdk | — |
| Resilience | tenacity | 9 |
| Model providers | Google Gemini, recorded (offline) | — |

**Quality tooling:** ruff, mypy (strict on `domain` and `services`),
import-linter, pytest, hypothesis, playwright, bandit, pip-audit,
detect-secrets, mutmut, nox, pre-commit, cyclonedx (SBOM), and a hash-pinned
`requirements.lock`.

---

## 8. Data and the demo portfolio

No live core-banking integration is required.

**The demo book is real companies on their real numbers.** The 24 demo
borrowers are companies listed on the National Stock Exchange, one per demo
industry, and every statement line the covenant engine tests comes from the
quarterly results each company filed with NSE (XBRL): the profit and loss for
the eight quarters from September 2024 to June 2026, and the balance sheets
filed with each half-year and full-year result. The snapshot lives in
[`src/covenant_radar/demo/data/indian_companies.json`](src/covenant_radar/demo/data/indian_companies.json),
so the demo never fetches it at launch; `python scripts/fetch_indian_financials.py`
refreshes it after each results season. Every seeded statement line keeps the
link to the filing it came from.

| Line | From the filing |
|---|---|
| Revenue, finance cost, depreciation | As filed for the quarter |
| EBIT | Revenue + other income − total expenses + finance costs (before exceptional items and tax) |
| Total debt | Current + non-current borrowings (lease liabilities are filed separately and excluded) |
| Tangible net worth | Equity attributable to owners − goodwill − other intangible assets |
| Current assets / liabilities | As filed |

Listed companies publish a balance sheet only half-yearly, so a June or
December quarter is tested against the balance sheet filed three months
earlier; its provenance says so.

**What is illustrative.** A lender's private data cannot be real in a public
demo, so it is labelled illustrative and kept neutral:

- **Covenants:** one standard package for every borrower — leverage (total
  debt / tangible net worth) at most 3.00x, interest cover at least 1.50x and
  current ratio at least 1.20x — built through the *real* registry and engine.
  The roster only includes companies that met all three on their September
  2024 results, as a lender requires at sanction, so a breach on screen is a
  company's published numbers deteriorating since then. Nothing is tuned per
  company.
- **Facilities:** sized at 5% of each company's latest reported borrowings
  (minimum ₹25 crore), as one lender in a consortium.
- **Repayment conduct and behavioural signals:** every company has clean
  repayment conduct. Only the five lender-side signal families (account
  activity, payment, utilisation, treasury, concentration) are seeded, with
  unremarkable values marked synthetic; no industry or news reading is invented
  for a real company — real news and market data are on **Market
  intelligence**. The demo never attributes an overdue, an SMA status or
  adverse conduct to a real company. The walkthrough's **Simulate a signal**
  scenarios are the only way one appears, and they are labelled synthetic.
- **Contacts:** the company secretarial desk at a non-deliverable `.invalid`
  address; no individual is named and no notification can reach a real inbox.

The queue in the demo launcher states this in its header. The seed is safe to
re-run: existing periods, covenant versions and tests are detected before
insert, and identities and facility limits are only rewritten when they differ.

**Reference portfolio.** Underneath the demo book, the product still ships its
deterministic synthetic Indian commercial-lending generator (seeded via SHA-256,
never process randomness or the wall clock). It provides the facility and
master-data skeleton the demo seed renames, and it is what the ML challenger is
trained on and the G1/G3 acceptance checks are scored against. Larger synthetic
portfolios can be generated with an explicit `ReferencePortfolioConfig`.

**External signals** come from a documented synthetic feed adapter, so feed
polling, entity resolution and the review queue are all exercised before any
licensed news or bureau subscription exists.

**Labelled cohorts** in the reference build (deteriorating, noisy-transient,
stable) are what the G1/G3 acceptance checks are scored against.

---

## 9. AI usage and governance

Every outbound model call goes through **one guarded call site**. There is no
second path.

- **Masking, fail-closed.** Only derived values and the small amount of clause
  text stage 1 needs may leave. Containers are flattened recursively before
  their leaves are validated; names and official identifiers are masked;
  configured secrets are redacted. The returned prompt carries a marker the
  client checks, so a caller cannot substitute an unmasked prompt by accident.
  The host-only token map is never part of what a provider sees.
- **Registry guard.** The call site consults the model registry first. No
  registration, or a registration that is not (or no longer) approved, is
  refused. Only the literal environment `development` relaxes this — anything
  else, including an unset variable, is treated as production.
- **Budget ceilings (T7).** Capacity is reserved before the provider is
  invoked: calls per hour, calls per day and an optional monthly budget.
- **Prompt versioning.** Prompts are files with recorded hashes; the version
  is verified at the boundary and stored with the call.
- **One retry, then stop.** Provider adapters are deliberately small and never
  retry on their own.
- **An append-only record for every attempt or refusal** — including calls
  that never happened because a guard refused them.
- **Model cards** for `stage1_extraction`, `stage4_forecast_ml` and
  `stage7_memo` state purpose, controls and explicit non-purposes.

**Failure is a designed state.** A provider outage, a bad-shape reply or a hit
ceiling returns an explanatory block with HTTP 200 and leaves the rest of the
workspace intact. It never becomes a claim that a model response was received.

---

## 10. Security and compliance

- **Authentication:** Argon2 password hashing, TOTP MFA, OIDC and SAML SSO.
- **Sessions:** signed HttpOnly cookies; `Secure` set automatically when the
  listener is not on loopback.
- **Authorization:** 33 permissions across 8 roles, resolved server-side, plus
  row-level portfolio scoping applied in SQL at every query boundary — carried
  even on child queries after the parent has already been resolved.
- **Maker-checker:** a shared approval workflow used by covenant registration,
  threshold changes and model promotion. The approve control is withheld from
  a proposal's own maker even when they hold the permission, because the
  distinct-actor rule is a UI promise as well as a database constraint.
- **Field encryption:** documents and identifying fields are encrypted before
  persistence; the authenticated envelope is verified on read. CIN lookup uses
  an **HMAC fingerprint** — deterministic and non-reversible — and search
  **never** falls back to plaintext matching.
- **Secrets:** read only from the real process environment or the OS keyring —
  deliberately never from `.env` or a config file. The application never
  silently creates or persists a secret.
- **Uploads:** preflight validation, content scanning, and a document
  classifier before a file can become evidence.
- **Also:** CSRF protection, security headers with CSP, rate limiting, log
  redaction, and archive-safety checks in the bundle verifier.

---

## 11. Quality bar

**Test suite:** ~200 test modules across unit, property (Hypothesis),
integration, contract, migration, end-to-end (Playwright), accessibility,
security and performance. They cover the pure ratio and forecast logic,
persistence and migrations, authentication and scoping, document encryption,
AI masking and cassette replay, route contracts, browser screens, audit
reconstruction and pipeline idempotency.

**Offline evaluation:** 35 authored examples scored across ten categories —
`extraction`, `engine`, `boundary`, `persistence`, `materiality`,
`forecast_dating`, `false_escalation`, `grounding`, `refusal`, `usefulness`.
A **permanent baseline arm** is scored through the *same* module as the
product arm, so an arm cannot quietly change its own metric, and a skipped
example is never turned into a zero. Per-category floors are recorded in
`evaluation/floors.json` with justification and history.

**The gate:** `radarctl gate` runs fourteen ordered steps.

```powershell
python -m radarctl gate --fast   # format, lint, types, import-contracts,
                                 # unit/property tests, alembic drift
python -m radarctl gate          # + integration, contract, seed determinism,
                                 # evaluation, e2e, a11y, security, performance
```

**Seed determinism and byte-for-byte reproducibility** are themselves gate
steps: a CRILC report regenerated for a past date must reproduce the original
byte for byte for all non-timestamp content.

---

## 12. Running it

### Cross-platform judge demo

With Python 3.12+ and the project dependencies installed:

```sh
python scripts/demo_up.py
```

Open http://127.0.0.1:8000 and sign in as `riskhead` with `CovenantRadar#2026`.
The launcher trains the synthetic ML challenger, applies migrations, seeds a
fresh isolated database, creates personas and runs the real nightly pipeline.
Use `--port 8001` if needed, or `--without-ml` for a faster rules-only rehearsal.
Each launch starts fresh; encryption keys live only in that process. Keep it
running during the presentation.

Open **Market intelligence** in the sidebar to fetch real public data. The first
visit fills within about 10–20 seconds, and no API key is needed. Market data is
live, and borrower financials are the companies' own filed results; import
statements through the existing financial-statement workflow to evaluate your own
borrowers.
Use `--offline-data` to disable public requests.

Gen AI uses offline recordings. Only exact matching prompts can be replayed;
arbitrary borrower memos need a configured, approved live provider. The screen
explains a missing recording explicitly. See [the judge runbook](judge-demo.md)
and [the review findings](repo-review.md) before rehearsing.

### Fastest path: the prepared demo

PowerShell is the supported local demo shell. From the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo_up.ps1
```

This installs the package, applies migrations, loads the reference portfolio
with its signal stream, seeds the demo borrowers with three covenants each,
creates the personas, runs the real six-step nightly pipeline, and starts the
UI at `http://127.0.0.1:8000`.

The bootstrap is deliberately hermetic: it forces SQLite, local encrypted
document storage and **recorded** model responses, so it can never seed an
external database or send a billable request just because the caller has those
variables set. `-LiveModel` is the one explicit way in.

For a faster rebuild:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo_up.ps1 -SignalDays 90
```

### Demo logins

One account per role, all with the password `CovenantRadar#2026`: `riskhead`,
`risk`, `credit`, `approver`, `rm`, `auditor`, `admin`, `steward`.

**Start with `riskhead`** — it is the only single login that reaches the whole
30/60/90 flow end to end.

The password is documented on purpose: these accounts are for a disposable
local showcase. Change them before exposing the app beyond localhost.

A timed presenter script is in
[the demo runbook](docs/demo-runbook.md).

### Manual setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m radarctl migrate upgrade
python -m radarctl seed
python -m radarctl seed --reference-portfolio
python -m radarctl seed --demo-covenants
python create_user.py
python -m radarctl job run nightly.pipeline
python -m radarctl serve
```

Create the personas **before** running the pipeline — its notification step
resolves recipients, and a run against a user-less database produces no
notifications.

Copy `.env.example` to `.env` and set the session, field-encryption and
CIN-fingerprint secrets. Because `security.secrets` reads only from the real
process environment or the OS keyring, a bare `python -m radarctl serve` in a
fresh shell fails startup with `SecretLoadError` even with a populated `.env`.
Load `.env` into the shell first, or use the wrapper that does it for every
`serve` call:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\serve_local.ps1
```

### Deployment

```powershell
python -m pip install --require-hashes -r requirements.lock
python -m radarctl migrate upgrade
python -m radarctl serve --host 0.0.0.0 --port 8000 --workers 2
```

Use PostgreSQL. Terminate TLS at the ingress. Inject secrets from the platform
secret manager. Restrict database credentials to the required application
tables. Rotate session and encryption keys through the documented key
lifecycle. Send structured logs and metrics to the platform observability
system. Do not use the demo accounts or demo secrets in a shared environment.

---

## 13. Command reference

```powershell
python -m radarctl --help
python -m radarctl serve [--host --port --workers]
python -m radarctl migrate upgrade|check|current
python -m radarctl seed [--reference-portfolio] [--demo-covenants]
                        [--signal-days N] [--check-deterministic] [--reset]
python -m radarctl user create
python -m radarctl job run nightly.pipeline --as-of 2026-09-01
python -m radarctl job run nightly.test          # or any single step
python -m radarctl cassette record|replay
python -m radarctl bundle verify
python -m radarctl gate [--fast]
```

### Configuration

Validated once through Pydantic settings. Environment values override
`config/default.toml`; **secrets are accepted from environment or keyring and
rejected from TOML files.**

| Setting | Purpose |
|---|---|
| `COVENANT_RADAR_DATABASE__URL` | SQLite for the demo, PostgreSQL deployed |
| `COVENANT_RADAR_SECURITY_SESSION_SECRET` | Browser session signing key |
| `COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY` | Document and field encryption |
| `COVENANT_RADAR_SECURITY_CIN_FINGERPRINT_KEY` | HMAC key for CIN lookup |
| `COVENANT_RADAR_DOCUMENTS__STORE` | `none` or encrypted `local` (S3 is refused until its adapter is configured) |
| `COVENANT_RADAR_AI__PROVIDER` | `none`, `recorded`, or a configured live provider |
| `COVENANT_RADAR_FORECAST__*` | Horizons, scoring weights, ML mode and artifact path |
| `COVENANT_RADAR_ENVIRONMENT` | Only the literal `development` relaxes the model-registry guard |

Risk thresholds **T1–T12** live in `config/thresholds.default.json` and are
versioned as snapshots, so every scoring run is pinned to the snapshot that
was active when it started — a threshold change mid-run cannot alter it.

---

## 14. Repository map

```
src/covenant_radar/
  domain/        Pure rules: ratios, covenants, signals, forecast,
                 interventions, triage, cases, certificates, memo, statements
  services/      Use cases and transaction boundaries (38 services)
  db/            Models, repositories, migrations, seeds
  ai/            The guarded call site, masking, budget, registry,
                 prompts, providers
  ml/            Local scikit-learn training and inference
  api/           Versioned REST API (/api/v1)
  web/           Routes, templates, view models, static assets
  ingestion/     Statement readers/normalisation, signal sources, feeds
  audit/         Hash chain, trace reader, reconstruction, bundles
  security/      RBAC, sessions, crypto, MFA, SSO, maker-checker, uploads
  scheduler/     Job registry, runner, ledger, policy, nightly pipeline
  reporting/     CRILC, Board MIS, EWS/RFA pack
  notifications/ In-app, email, digest, webhook
  observability/ Health, logging, metrics, redaction, retention
evaluation/      Offline evaluation harness, reference portfolio, cassettes
tests/           unit · property · integration · contract · migration ·
                 e2e · a11y · security · perf
docs/            Demo runbook, ADRs, API reference, model cards, calibration
config/          default.toml, logging.toml, thresholds.default.json
scripts/         demo_up.ps1, serve_local.ps1, UI capture, contrast check
```

---

## 15. Honest limits

Worth stating plainly, because the product's own design principle is that a
number should not be presented as more than it is:

- **The probability is a calibrated risk score, not a statistical certainty.**
  The G1/G3 results above come from the synthetic reference portfolio. A
  customer must repeat the calibration procedure against their own labelled
  history and keep their own record.
- **The ML challenger runs in shadow mode.** The deterministic forecast is
  what the queue, the band and the case are built from.
- **There is no live core-banking connector.** The connector framework's seams
  exist and default to "nothing available" — an honest, observable outcome
  (zero events ingested; a covenant left untested with a recorded reason)
  rather than a fabricated one.
- **The S3 document adapter is refused**, not stubbed, until it is configured.
- **Recommendations are advisory.** Material credit, waiver and escalation
  decisions require human review, and the product says so on the memo.

---

## Further reading

- [`docs/demo-runbook.md`](docs/demo-runbook.md) — the timed presenter path
- [`docs/calibration/reference-portfolio.md`](docs/calibration/reference-portfolio.md) — the recorded threshold calibration
- [`docs/api/README.md`](docs/api/README.md) — API authentication, pagination, deprecation policy
- [`docs/model-cards/`](docs/model-cards/) — stage 1, stage 4 ML, stage 7
- [`docs/adr/`](docs/adr/) — CI and offline gates; field encryption and rotation; typography and theming
- [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CHANGELOG.md`](CHANGELOG.md)

## License

See [`LICENSE`](LICENSE).


### Gemini configuration

Set `GEMINI_API_KEY` in your environment or local `.env` to enable Gemini.
The default model is `gemini-3.8-flash`; override it with
`COVENANT_RADAR_AI__MODEL`. Requests go directly to Google’s
[Gemini API](https://ai.google.dev/gemini-api/docs/openai), without a proxy.
To explicitly select it, set `COVENANT_RADAR_AI__PROVIDER=gemini`.
Without a key, the default configuration leaves AI disabled; recorded
responses remain available for offline demos and tests.
