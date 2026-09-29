# Model card — `stage4_forecast_ml`

## Purpose

Produces 30/60/90-day model estimates from the allow-listed, point-in-time
structured Stage-4 feature snapshot. It never
calculates covenant compliance, a crossing date, a simulation, or a credit
decision.

## Controls

The local artifact is verified against its companion manifest before loading.
Both files must come from a trusted source: a checksum is not a signature, and
pickle artifacts must never come from untrusted uploads. Champion selection
requires approval of the exact `scikit-learn` / `sha256:<full digest>` identity.
The deterministic Stage-4 risk score is retained as a fallback for
missing features, unavailable artifacts, or a governance rollback. Every
prediction records artifact version and immutable feature snapshot hash in the
Stage-4 trace; identifiers, text and post-score outcomes are excluded. Calibrated
ensembles do not expose the first fold's coefficients as explanations of the
ensemble probability.

## Included reference model and limitations

`python -m evaluation.ml_reference` trains logistic and gradient-boosted
challengers on synthetic utilisation observations. It writes artifacts,
manifests and a report under `var/ml-reference/`. The report uses a chronological
split with borrower holdout, but does not purge overlapping outcome windows.
Calibration uses ordinary three-fold cross-validation. Weekly sampling reduces
near-duplicates; it does not establish temporal independence.

The estimates are not validated on real borrowers or on leverage/liquidity
covenants. Calibration fitting is not evidence of calibration in those domains.
Synthetic reference versions are therefore forced into shadow mode even if a
registry entry is approved. Shadow estimates do not determine the operational
action band. The operational rules themselves produce a prioritisation score,
not a statistically validated breach probability.

Promotion of a real model requires representative outcome data, purged temporal
evaluation, covenant-specific validation, calibration and subgroup analysis,
and approval tied to its artifact. The seeded governance scoreboard is
illustrative and is not an evaluation of the selected artifact.
