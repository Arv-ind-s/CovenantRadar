# Judge walkthrough

## Start and rehearse

Set `GEMINI_API_KEY` in your environment or local `.env`. Activate the Python
3.12 environment and run `python scripts/demo_up.py --check-only` first, then
`python scripts/demo_up.py`. The default is live Gemini 3.8 Flash. The preflight
checks real covenant extraction and grounded memo generation, and validates
PDF/DOCX generation, PDF text extraction and upload validation. It stops on
failure; it never silently switches to recorded AI.
On this Mac the installed environment is `.venv/bin/python`.
Open http://127.0.0.1:8000. Login: `riskhead` / `CovenantRadar#2026`.
Keep the launcher running. A new launch creates a fresh demo database, so
changes made during a rehearsal do not carry into the next presentation.

The launcher selects the model it actually trained; no manually copied artifact
filename is required. The 24 borrowers are NSE-listed companies, tested on the
quarterly results they filed with the exchange; covenant terms, facility limits
and bank-side signals are illustrative. Initial repayment conduct is clean;
explicit payment simulations introduce clearly labelled synthetic overdues.
Live Gen AI sends masked structured inputs to Google using `GEMINI_API_KEY`.
For offline rehearsal only, pass `--offline-ai`; arbitrary extraction and memos
are not supported by the authored replay cassettes. Train/setup output includes the synthetic ML
report; the report is also saved to `var/ml-reference/report.json`.

The queue shows the last completed scan, the next nightly scan, public-source
health, and a scoped feed of stored signal and scoring changes. With the
`riskhead` login, open the scenario menu, select a borrower and one of nine
scenarios, then choose **Run scenario**. Seven sustained scenarios cover
payment, account activity, utilisation, treasury, concentration, industry and
news; there is also a combined scenario and a temporary news spike. All pass
through real ingestion, evidence filtering and the nightly scoring pipeline.
The temporary spike tests noise suppression: it does not contribute sustained
forecast pressure. The payment scenario also records synthetic 40-day overdue
conduct, so SMA status agrees with the injected events.

Wait for the completed run in the queue before comparing previous/current
outcomes. A scenario can raise pressure without changing a borrower's band;
the effect depends on actual covenant headroom. Repeating the same scenario
for the same borrower and date inserts no duplicate observations or notices.
Use a fresh launch to restore the baseline. Do not promise a particular band
change based on an outdated company name or fixed score.

The notification centre is per person and limited to the portfolios each
person covers. After every review date, each risk head, risk analyst and
relationship manager gets one morning summary of their portfolios. A borrower
moving into a worse band reaches the people who cover it. Case alerts, SLA
breaches (an overdue case escalates), mentions and approval expiries go to the
person concerned. A failed nightly job alerts the administrators.

This button is disabled outside the demo launcher by default. The normal
schedule remains nightly; the demo action is an explicit manual trigger, not a
claim that bank feeds are connected continuously.

The seeded statements are each company's eight filed quarters from September
2024 to June 2026, labelled on the April-to-March financial year, from the
snapshot in `src/covenant_radar/demo/data/indian_companies.json`. Every
statement line links to the NSE filing it came from. A quarter is overdue 60
days after its successor ends, so the June 2026 quarter stays current until
29 November 2026; after the September 2026 results are filed (by mid-November),
run `python scripts/fetch_indian_financials.py --latest-quarter 2026-09-30` to
roll the book forward. The illustrative signal history runs to the day before
launch.

## Five-minute story

1. **Problem, 30 seconds:** “A covenant can look healthy at the last filing and
   deteriorate before the next one. Credit teams need an early warning they can
   explain and act on.”
2. **Prioritise, 45 seconds:** Open the queue. Show the action bands, exposure,
   and one borrower. Explain that every number comes from a completed run.
   The book is 24 real NSE-listed companies; point out that every figure
   traces to the company's own exchange filing.
3. **Explain, 75 seconds:** Open the borrower forecast, choose 30/60/90 days,
   and open “Why this forecast?”. Show the contractual threshold, crossing,
   input evidence and source references. Open “How to read this forecast” to
   distinguish the rules, ML estimate, confidence and Gen AI.
4. **Act, 60 seconds:** Use “Run simulation” from the borrower. Select an
   applicable intervention, read its assumptions and compare against doing
   nothing. The result is a scenario, not evidence that the intervention will
   causally produce that outcome.
5. **Control, 60 seconds:** Show the case history and governance screen.
   Explain maker-checker approval, exact artifact identity and shadow mode.
   The seeded governance scoreboard is an illustrative approval scenario;
   use the evaluation output for measured results.
6. **Close, 30 seconds:** “We join warning, evidence and an accountable next
   action in one workflow, while keeping contractual arithmetic inspectable.”

## Gen AI demonstration

Live Gemini is the launcher default and is checked before the server starts.
Use **Generate AI explanation** on the borrower and inspect its citations,
then export the resulting memo as PDF and DOCX. Download the generated
sanction-letter PDF from the queue's demo controls for the intake workflow.
It contains a DSCR minimum of 1.25x, tested quarterly, with a 30-day cure.
Upload it, inspect the extracted clause and perform human verification and
approval before treating it as a contractual covenant. This sample is synthetic.

The explicit `--offline-ai` mode covers authored evaluation examples only.
A cassette miss is reported clearly; repeated clicks do not create recordings.
Never describe that mode as a live model call. `--offline-data` independently
disables public market requests for a disconnected rehearsal.

Run `python -m evaluation.run --both-arms --gate --floors evaluation/floors.json`
to demonstrate the authored extraction, grounding, refusal and forecast checks
offline. Passing those cases does not establish production accuracy.

## Likely questions

- **“Is the displayed 99% statistically calibrated?”** The operational rules
  produce a prioritisation score. The synthetic challenger supplies a separate
  estimate; real portfolio validation is still required.
- **“Why keep ML in shadow?”** The included training example uses synthetic
  utilisation observations. It is not validated across the other covenant
  types. Synthetic reference artifacts cannot become champion in the nightly
  runtime.
- **“How do you stop hallucinations?”** Masked, structured evidence; numeric,
  action and output checks; cited records; and an explicit refusal path. These
  are useful controls, not proof of semantic correctness for every sentence.
- **“What is running on AWS?”** This checkout has no Amazon Bedrock provider or
  deployment proof. Describe AWS deployment as planned unless you deploy and
  verify it separately.
- **“Can it export a memo?”** DOCX and PDF code exists. PDF requires WeasyPrint's
  native libraries on the presentation machine. The startup preflight renders a PDF and
  DOCX and checks native PDF text and upload acceptance. The launcher detects
  Homebrew's library path. A generated memo is still needed before exporting;
  recorded mode cannot generate arbitrary borrower memos.

## Local verification

The PostgreSQL test connection uses `RADAR_TEST_DATABASE_URL`, with a
`postgresql+psycopg://` URL, separately from application configuration. Use a
disposable test database. On this Apple Silicon Mac the full suite command is:

```sh
PATH="$PWD/.venv/bin:$PATH" \
RADAR_TEST_DATABASE_URL="postgresql+psycopg://USER@127.0.0.1:PORT/TEST_DATABASE" \
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \
.venv/bin/python -m pytest tests -q
```

Use Python directly: shell wrappers can remove macOS library-path variables.
`lint-imports` checks the layered architecture recorded in
`docs/adr/0004-layered-architecture.md`; all its contracts are kept.

For the full audit search, sign out and sign in as `auditor` with the same
demo password, then open `/audit`. Risk Head can review case history but does
not have the separate audit-search permission. Human approval must use a
different authorised checker from the person who submitted the proposal.
