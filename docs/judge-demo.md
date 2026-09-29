# Judge walkthrough

## Start and rehearse

Activate the Python 3.12 environment and run `python scripts/demo_up.py`.
On this Mac the installed environment is `.venv/bin/python`.
Open http://127.0.0.1:8000. Login: `riskhead` / `CovenantRadar#2026`.
Keep the launcher running. A new launch creates a fresh demo database, so
changes made during a rehearsal do not carry into the next presentation.

The launcher selects the model it actually trained; no manually copied artifact
filename is required. All data is synthetic. Gen AI is in recorded mode and
makes no live provider calls. Train/setup output includes the synthetic ML
report; the report is also saved to `var/ml-reference/report.json`.

The queue shows the last completed scan, the next nightly scan, public-source
health, and a scoped feed of stored signal and scoring changes. With the
`riskhead` demo login, choose a borrower in **Synthetic walkthrough borrower**
and select **Inject synthetic payment signal**. This uses the ingestion service
and submits the real pipeline; the borrower page compares the previous and
new stored outcomes. Refresh the queue after the run finishes to see the
changed band and case. This button is disabled outside the demo launcher by
default. The normal schedule remains nightly; the demo action is an explicit
manual trigger, not a claim that bank feeds are connected continuously.

## Five-minute story

1. **Problem, 30 seconds:** “A covenant can look healthy at the last filing and
   deteriorate before the next one. Credit teams need an early warning they can
   explain and act on.”
2. **Prioritise, 45 seconds:** Open the queue. Show the action bands, exposure,
   and one borrower. Explain that every number comes from a completed run.
   In this checkout the curated book contains 24 borrowers, not 5,000.
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

The bundled cassette set covers authored evaluation examples; it does not cover
every borrower, evidence snapshot or simulation. A cassette miss now says this
explicitly. Repeated clicks cannot produce a new recording.

For a live memo demonstration, configure a supported live provider separately
(Anthropic, Azure OpenAI or TCS GenAI Lab) and approve its exact provider/model/
prompt configuration. The isolated launcher intentionally ignores deployment
credentials. Rehearse the exact borrower and action sequence before presenting.
Do not describe an offline replay as a live model call.

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
  native libraries on the presentation machine. Both export formats passed
  integration checks on this Mac after installing Pango/GObject. The launcher
  detects Homebrew's library path. A generated memo is still needed before
  exporting; recorded mode cannot generate arbitrary borrower memos.

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
See `repo-review.md` for the remaining architecture gate failure.
