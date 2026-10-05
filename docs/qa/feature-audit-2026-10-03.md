# Feature audit — 3 October 2026

Checked the current working tree, including its pre-existing changes. No application code was changed by this audit.

## Findings

1. **PDF memo export fails under the default local Python environment.** Three export integration tests fail because WeasyPrint cannot load `libgobject-2.0-0`. The library is installed under `/opt/homebrew/lib`; all six memo-export tests pass with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`. This is a runtime configuration issue, not a confirmed export-logic defect. The cross-platform demo launcher already supplies this path; direct Python launches need it too.
2. **Two architecture checks need the virtual environment on PATH.** Running `.venv/bin/python` alone does not make `lint-imports` available to child processes. All five gate tests pass with `PATH="$PWD/.venv/bin:$PATH"`.
3. **PostgreSQL behavior remains unverified.** Two database tests and the PostgreSQL prerequisite check cannot run without `RADAR_TEST_DATABASE_URL`. SQLite-backed tests do not establish PostgreSQL correctness.

No reproducible feature-logic bug was found in the checks completed here. This does not establish that every workflow is defect-free.

## Validation

- Unit and integration: 1,283 passed initially; two gate failures and three PDF failures subsequently passed with the environment corrections above. Three PostgreSQL checks remain blocked.
- Browser/end-to-end, API contracts, and property tests: 91 passed.
- Security and accessibility: 106 passed.
- Total distinct passing tests after targeted reruns: 1,485.
- Authenticated Chromium smoke check against the existing demo on port 8011: main navigation, queue band filters, borrower pages, case detail links, forecast explanations, and memo deep links. No HTTP error or uncaught JavaScript error on the successfully checked links.

The browser smoke check used the risk-head demo persona and read-only navigation. Existing workflow tests exercised intake, queue controls, cases, simulator, scoring, notifications, governance, and authorization. This audit did not manually complete every write workflow, call live external providers, or run performance and migration suites.

## Reproduction commands

```sh
.venv/bin/python -m pytest tests/unit tests/integration -q --tb=short
.venv/bin/python -m pytest tests/e2e tests/contract tests/property -q --tb=short
.venv/bin/python -m pytest tests/security tests/a11y -q --tb=short
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/unit/test_gate.py -q
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest tests/integration/test_memo_export.py -q
```
