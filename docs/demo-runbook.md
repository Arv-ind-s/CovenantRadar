# Covenant Radar presentation runbook

Use [judge-demo.md](judge-demo.md) for the current walkthrough, data boundaries
and likely questions. Historical fictional borrower names and fixed scores
are not presentation fixtures; choose from the current 24-company queue.

1. Set `GEMINI_API_KEY` locally. Run `.venv/bin/python scripts/demo_up.py --check-only`.
   A failed check must be resolved before the live presentation. This invokes
   real Gemini extraction and grounded memo generation and validates PDF/DOCX
   output and the upload guard. It can incur Gemini usage charges.
2. Run `.venv/bin/python scripts/demo_up.py`. On Windows use
   `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo_up.ps1`.
   Both launchers use the same implementation and default to Gemini 3.8 Flash.
3. Open `http://127.0.0.1:8000`, sign in as `riskhead` with
   `CovenantRadar#2026`, and keep the launcher running. The database is isolated
   and disposable; restarting resets the rehearsal.
4. Show the ranked queue, dated forecasts, evidence, the seven signal families
   and visible explanation of risk score versus shadow ML and confidence.
5. Run a sustained scenario through the queue, wait for the scoring run, and
   compare stored outcomes. Contrast the temporary news spike with sustained
   news to show noise filtering. All injected observations are synthetic.
6. Run an intervention simulation, compare it with doing nothing, and show its
   assumptions and the case action/audit history.
7. Generate the grounded Gemini memo, inspect its citations and export PDF and
   DOCX. Download the intake PDF from the queue controls; upload it and inspect
   and verify the proposed DSCR covenant before approving it.

For offline rehearsal, add `--offline-ai --offline-data`. Recorded AI supports
only authored cassette examples and cannot draft arbitrary company memos;
this mode is explicitly labelled and must not be used to claim live Gemini.
The initial seeded company results link to public NSE filings. Covenant terms,
facility exposure and bank-side observations are illustrative and are not
allegations about any named company. Synthetic ML remains in shadow mode.

For the full audit search, sign out and sign in as `auditor` with the same
demo password, then open `/audit`. Risk Head can review case history but does
not have the separate audit-search permission. Human approval must use a
different authorised checker from the person who submitted the proposal.
