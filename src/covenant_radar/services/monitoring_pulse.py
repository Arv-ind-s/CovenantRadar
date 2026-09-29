"""Small, scoped read models for the monitoring surface.

Every displayed activity item comes from a persisted signal, forecast run, or
job.  The browser never invents monitoring events from a timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import Forecast, ForecastRun, TriageEntry
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.models.workflow import Case
from covenant_radar.db.scoping import Scope


def monitoring_status(
    session: Session, scope: Scope, *, source_health: dict | None = None
) -> dict[str, object]:
    """Describe actual processing health; a browser connection is not a scan."""
    latest = session.scalar(
        select(JobRun).where(JobRun.job_name == "nightly.pipeline")
        .order_by(JobRun.started_at.desc()).limit(1)
    )
    successful = session.scalar(
        select(JobRun).where(JobRun.job_name == "nightly.pipeline", JobRun.state == "succeeded")
        .order_by(JobRun.finished_at.desc()).limit(1)
    )
    latest_run = session.scalar(
        select(ForecastRun).where(
            ForecastRun.finished_at.is_not(None), ForecastRun.state == "complete"
        )
        .order_by(ForecastRun.finished_at.desc()).limit(1)
    )
    borrowers_processed = None
    if latest_run is not None:
        borrowers_processed = session.scalar(
            select(func.count()).select_from(TriageEntry)
            .join(Borrower, Borrower.id == TriageEntry.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(TriageEntry.run_id == latest_run.id, scope.predicate(Portfolio.path))
        )
    sources = tuple((source_health or {}).get("sources", ()))
    current_sources = sum(1 for source in sources if source.get("status") == "Current")
    return {
        "last_scan": successful.finished_at if successful else None,
        "latest_run": latest_run,
        "state": latest.state if latest else "never_run",
        "next_scan": _next_scan(datetime.now(UTC)),
        "sources_current": current_sources,
        "sources_total": len(sources),
        "source_status": (
            "Source health available" if sources else "No public source health available"
        ),
        "processed": borrowers_processed,
    }


def _next_scan(now: datetime) -> datetime:
    # The registered pipeline schedule is 01:00 UTC (nightly_runtime.py).
    next_tick = now.replace(hour=1, minute=0, second=0, microsecond=0)
    return next_tick if next_tick > now else next_tick + timedelta(days=1)


def recent_changes(
    session: Session, scope: Scope, *, limit: int = 8
) -> tuple[dict[str, object], ...]:
    """A compact source-to-decision trail within the caller's portfolio scope."""
    if scope.is_empty:
        return ()
    ranked = select(
        SignalEvent.id.label("event_id"),
        func.row_number().over(
            partition_by=SignalEvent.borrower_id,
            order_by=(SignalEvent.ingested_at.desc(), SignalEvent.id.desc()),
        ).label("position"),
    ).subquery()
    events = session.execute(
        select(SignalEvent, Borrower)
        .join(ranked, ranked.c.event_id == SignalEvent.id)
        .join(Borrower, Borrower.id == SignalEvent.borrower_id)
        .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
        .where(scope.predicate(Portfolio.path), ranked.c.position == 1)
        .order_by(SignalEvent.ingested_at.desc(), SignalEvent.id.desc())
        .limit(limit)
    ).all()
    rows: list[dict[str, object]] = []
    for event, borrower in events:
        latest = session.execute(
            select(TriageEntry, ForecastRun)
            .join(ForecastRun, ForecastRun.id == TriageEntry.run_id)
            .where(
                TriageEntry.borrower_id == borrower.id,
                ForecastRun.finished_at.is_not(None),
                ForecastRun.state == "complete",
            )
            .order_by(ForecastRun.finished_at.desc())
            .limit(1)
        ).first()
        triage, run = latest if latest else (None, None)
        after_signal = bool(
            run
            and run.started_at >= event.ingested_at
            and run.as_of_date >= event.event_date
        )
        case = session.scalar(
            select(Case).where(Case.borrower_id == borrower.id)
            .order_by(Case.created_at.desc()).limit(1)
        )
        rows.append({
            "id": str(event.id),
            "borrower": borrower.legal_name,
            "reference": borrower.reference,
            "family": event.family.replace("_", " ").title(),
            "observation": _observation(event),
            "ingested_at": event.ingested_at,
            "signal_href": f"/borrowers/{borrower.reference}#case-signals",
            "forecast_href": f"/borrowers/{borrower.reference}#case-forecast",
            "status": "Scored" if after_signal else "Awaiting next scan",
            "band": triage.band if after_signal and triage else None,
            "change": triage.what_changed if after_signal and triage else None,
            "case_href": f"/cases/{case.reference}" if case and after_signal else None,
            "case_state": case.state.replace("_", " ").title() if case and after_signal else None,
            "synthetic": bool(
                event.payload.get("demo_version") or event.payload.get("synthetic_walkthrough")
            ),
        })
    return tuple(rows)


def borrower_risk_comparison(session: Session, borrower_id: UUID) -> dict[str, object]:
    """Read two stored queue outcomes for a borrower, never recompute a score."""
    rows = session.execute(
        select(TriageEntry, ForecastRun)
        .join(ForecastRun, ForecastRun.id == TriageEntry.run_id)
        .where(
            TriageEntry.borrower_id == borrower_id,
            ForecastRun.finished_at.is_not(None),
            ForecastRun.state == "complete",
        )
        .order_by(ForecastRun.finished_at.desc())
        .limit(2)
    ).all()
    def snapshot(index: int) -> dict[str, object] | None:
        if len(rows) <= index:
            return None
        entry, run = rows[index]
        forecast = (
            session.scalar(
                select(Forecast).where(
                    Forecast.run_id == run.id,
                    Forecast.covenant_version_id == entry.worst_covenant_version_id,
                    Forecast.horizon_days == entry.worst_horizon,
                ).limit(1)
            )
            if entry.worst_covenant_version_id is not None and entry.worst_horizon is not None
            else None
        )
        source = forecast.probability_source if forecast is not None else "unknown"
        formula = forecast.formula_inputs or {} if forecast is not None else {}
        ml_prediction = formula.get("ml_prediction") if isinstance(formula, dict) else None
        predictor_mode = formula.get("predictor_mode") if isinstance(formula, dict) else None
        ml_model_version = (
            ml_prediction.get("model_version") if isinstance(ml_prediction, dict) else None
        )
        return {
            "band": entry.band or "Unranked", "score": _percent(entry.probability),
            "at": run.finished_at, "run_id": str(run.id),
            "model_version": run.model_version,
            "source": source,
            "predictor_mode": predictor_mode,
            "ml_model_version": ml_model_version,
        }
    return {"current": snapshot(0), "previous": snapshot(1)}


def _percent(value: Decimal | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "Unavailable"


def _observation(event: SignalEvent) -> str:
    if event.magnitude is None:
        return "Observation received"
    return f"{format(event.magnitude.normalize(), 'f')} {event.unit or ''}".strip()
