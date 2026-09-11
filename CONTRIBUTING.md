# Contributing to Covenant Radar

1. **Read** the block and only the paths its `Read first` names, plus `plan.md §6` for the contracts it lists.
2. **Branch** `task/t-0NN-<slug>` from the current main line; record the base commit.
3. **Build** inside `Files owned`. Write the named tests as you go.
4. **Prove** with `python -m radarctl gate --fast`, then each `Run` command with its stated expected result.
5. **Hand over** with the task id, the commands run, their output, and anything the block did not anticipate.
6. **Wait** for human review and merge. Never merge your own work.
7. **Record** in `MERGE_LOG.md`: task id, commit, revert command, plan days, actual days.

## Adding a dependency

A new third-party component is a licence obligation, an SBOM row and a
`pip-audit` surface, so it is reviewed as one:

1. Declare it in `pyproject.toml` — under `[project].dependencies` only if it
   ships, otherwise under the `dev` extra.
2. Recompile the lockfile:
   `uv pip compile pyproject.toml --all-extras --output-file requirements.lock`.
3. Resolve the licence of every package the diff adds — transitive ones
   included — and classify it against the policy in
   [`docs/oss-ip-compliance.md` §3](docs/oss-ip-compliance.md#3-licence-policy).
   Anything outside **Permissive** needs a written position before merge;
   strong copyleft (GPL, AGPL) does not enter the distributable.
4. Add the row to the register, and the notice to
   [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) if it ships.

Vendoring an asset instead of depending on it does not skip this: record the
version, the licence and a digest next to the bytes, as
`web/static/vendor/htmx/README.md` and `web/static/fonts/LICENSES.md` do.
