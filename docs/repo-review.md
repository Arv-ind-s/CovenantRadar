# Presentation readiness review — 22 September 2026

## Fixed

| Area | Finding and change |
|---|---|
| ML integrity | The loader calculated a digest but never checked it. It now verifies the companion manifest before deserializing and checks artifact metadata. Only trusted local artifacts should be loaded; a manifest is not a signature. |
| ML governance | Component approval could be reused after replacing the artifact. Champion selection now requires `scikit-learn` and the exact registered `sha256:<full digest>` model ID. Synthetic reference versions remain shadow-only. |
| ML provenance | A frozen feature object contained a mutable dictionary. Snapshots now hold an immutable copy. Non-finite predictions are rejected explicitly. |
| ML explanation | The first calibration fold's coefficients were represented as contributions for the entire calibrated ensemble. That misleading attribution is no longer emitted. |
| Gen AI governance | Approval checked only a component name, and an omitted component could bypass the lookup. Calls now derive their component from the stage and match provider, model and prompt version against the approval. |
| Gen AI failure UX | A missing offline recording looked like a temporary provider outage. The memo now explains the missing recording and the available paths forward. Recorded drafts are labeled as offline replay. |
| Queue interaction | Clear selection retained a closure over detached rows after live polling replaced the table. It now resolves the current ledger; a browser regression test reproduces the swap. |
| Mobile navigation | Closing the menu restarted the content animation and briefly blanked the page. The entrance fade no longer transforms drawer ancestors or restarts when overlays close. |
| Live updates | Cursor parsing split a binary HMAC at a separator that could also occur inside the signature, causing intermittent duplicate activity. Parsing now uses the fixed digest length. |
| PostgreSQL | Fixed trigger DDL percent escaping and a migration that rebuilt referenced tables instead of altering their checks in place. Integration fixtures now use private schemas. |
| Logging | In-process migrations disabled existing application loggers. Migration setup now preserves them. |
| Simulator | Removed the raw JSON editing field from the presenter workflow while retaining the stored risk settings and action assumptions. |
| Forecast readability | Added an expandable explanation of operational risk scores, shadow ML, input confidence and Gen AI's separate role. |
| Governance truthfulness | Seeded scoreboard numbers now carry illustrative provenance and an explicit screen notice. They must not be cited as measured accuracy. |
| Demo startup | Added `scripts/demo_up.py` for macOS/Linux/Windows. It selects the freshly trained artifact, isolates the database, creates personas before governance approval, and runs the real pipeline. Base configuration no longer points to an absent artifact. |
| Quality checks | Enabled actual nox browser, accessibility and contract sessions, with complete application dependencies in test sessions. Corrected the CI PostgreSQL variable that application settings rejected and selected the installed psycopg driver. Browser discovery works beyond Windows. Added 403 accessibility coverage and consolidated the sign-in design tokens. Updated stale fixtures and isolated test models from migration metadata. |

## Verified

- The application starts, accepts the documented demo login and serves the
  queue, cases, simulator, governance, facilities, covenants, notifications and
  borrower pages successfully over local HTTP.
- A real intervention comparison was submitted and returned successfully.
- PDF and DOCX integration checks verify matching figures, full assumptions
  and stable integrity hashes with native PDF dependencies installed.
- Borrower forecasts contain persisted shadow ML predictions from the locally
  trained, verified artifact.
- The offline authored evaluation passes all ten category floors after the
  changes. These are 35 authored examples, not a real-world accuracy study.
- Desktop, tablet and mobile browser smoke checks pass, including navigation,
  dark mode, reduced motion and the live-refresh selection regression.
- The accessibility contract renders the current screen catalogue in both
  themes, including the previously missing 403 page. This is an automated
  markup audit, not a complete manual accessibility certification.

## Remaining limitations

1. **Live Gen AI is not configured or verified.** The recorded provider cannot
   draft arbitrary borrower memos. A supported live provider and a rehearsed
   approval/configuration are required for that demonstration.
2. **The ML backtest is illustrative.** It trains on synthetic utilisation
   data. Applying that artifact to leverage or liquidity covenants is outside
   the demonstrated training domain, even though the feature names match.
   The temporal split does not purge overlapping label windows, and calibration
   uses ordinary cross-validation. Do not claim production calibration, measured
   financial uplift, or readiness for champion promotion.
3. **Operational probability naming is broader than its evidence.** The
   deterministic output is a configured risk score; it has not been validated
   as a real borrower breach probability. The UI now explains that distinction.
4. **The import contracts were redefined, not refactored to.** The original
   ports-and-adapters contracts contradicted the service/ORM architecture the
   code was built on and failed on every run. `docs/adr/0004-layered-architecture.md`
   replaces them with the layering the code has, fixes its two real upward
   imports, and the contracts are now all kept. Services and screens still use
   SQLAlchemy directly; repository ports remain future work. There are also
   pre-existing lint debt and unfinished nox sessions outside those fixed
   here.
5. **Platform checks need dependencies.** PostgreSQL integration/migration
   tests require a running PostgreSQL service. PDF export needs native
   GObject/Pango libraries. These were installed and exercised on this Mac;
   other presentation machines need the same preparation.
6. **AWS integration is not established.** The provider list has no Bedrock
   implementation. This review did not provision or deploy AWS infrastructure.
7. **Semantic grounding still needs human review.** Numeric allow-list and
   shape checks do not prove every relationship stated in model prose.

No production data, live LLM requests, external notifications or AWS resources
were used for this review.

## Final validation

- **Full pytest run: 1,403 passed, 1 failed, 355 warnings** in 127.69 seconds.
  This includes the real Chromium browser checks, accessibility, security,
  contract, property, export, PostgreSQL and migration tests.
- The sole failure is `tests/unit/test_gate.py::test_import_contracts_parse`:
  four existing architecture contracts fail (service/adapter separation,
  presentation boundaries, SQLAlchemy session placement and audit writes).
  Domain purity and exclusive AI-provider imports remain enforced and pass.
  The contracts were not weakened to obtain a green run.
- The authored offline evaluation passes all ten category floors.
- Ruff lint and formatting pass for all 35 changed/new Python files.
  `git diff --check` passes. This is not a claim that repository-wide lint,
  typing or every nox gate is green.
- The refreshed app was checked over HTTP after the final code changes:
  login, nine screen requests, a real simulation and the explicit offline
  memo response all succeeded. It was left running on port 8000.

Local test tooling installed for this review: the Python `.venv`, Chromium,
Homebrew Pango and PostgreSQL 17. The temporary PostgreSQL test instance was
stopped after verification; no login-time database service was enabled.
