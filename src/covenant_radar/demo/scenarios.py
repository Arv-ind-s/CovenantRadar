"""Named illustrative observations sent through the real ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from covenant_radar.domain.signals import SignalEvent, definition_for


@dataclass(frozen=True, slots=True)
class DemoScenario:
    code: str
    label: str
    family: str | None
    values: tuple[str, ...]
    description: str


SCENARIOS = MappingProxyType(
    {
        scenario.code: scenario
        for scenario in (
            DemoScenario(
                "payment",
                "Sustained late payments",
                "payment",
                ("30", "35", "40"),
                "Three dated payment delays; inspect persistence, pressure and case priority.",
            ),
            DemoScenario(
                "account_activity",
                "Falling account inflows",
                "account_activity",
                ("-20", "-30", "-40"),
                "Three adverse changes in account activity.",
            ),
            DemoScenario(
                "utilisation",
                "Rising facility utilisation",
                "utilisation",
                ("80", "90", "98"),
                "Three observations of rising facility utilisation.",
            ),
            DemoScenario(
                "treasury",
                "Treasury outflow pressure",
                "treasury",
                ("1.0", "1.3", "1.6"),
                "Three dated observations of increasing cash outflow pressure.",
            ),
            DemoScenario(
                "concentration",
                "Increasing concentration",
                "concentration",
                ("40", "55", "70"),
                "Three observations of increasing group exposure concentration.",
            ),
            DemoScenario(
                "industry",
                "Sustained industry stress",
                "industry",
                ("0.5", "0.7", "0.9"),
                "Three synthetic industry deterioration observations.",
            ),
            DemoScenario(
                "news",
                "Sustained adverse news",
                "news",
                ("0.6", "0.7", "0.8"),
                "Three dated synthetic news observations, meeting the persistence threshold.",
            ),
            DemoScenario(
                "temporary_news",
                "One-off news: test noise suppression",
                "news",
                ("0.8",),
                "One adverse news observation. Alone it cannot meet the persistence threshold; "
                "existing sustained evidence remains visible.",
            ),
            DemoScenario(
                "combined",
                "Combined deterioration: all seven families",
                None,
                (),
                "All seven signal families enter ingestion and the same scoring run.",
            ),
        )
    }
)


def scenario_events(
    code: str, *, borrower_id: UUID, facility_id: UUID, today: date, actor_id: UUID
) -> tuple[SignalEvent, ...]:
    """Stable natural event keys make repeat clicks idempotent."""
    scenario = SCENARIOS[code]
    selected = (
        tuple(s for s in SCENARIOS.values() if s.family and len(s.values) == 3)
        if code == "combined"
        else (scenario,)
    )
    events: list[SignalEvent] = []
    for item in selected:
        assert item.family is not None
        definition = definition_for(item.family)
        for offset, value in enumerate(item.values):
            amount = Decimal(value)
            events.append(
                SignalEvent(
                    borrower_id=borrower_id,
                    facility_id=facility_id,
                    event_date=today - timedelta(days=len(item.values) - 1 - offset),
                    family=item.family,
                    event_type=definition.event_type,
                    magnitude=amount,
                    unit=definition.unit,
                    payload={
                        definition.value_field: int(amount)
                        if item.family == "payment"
                        else str(amount),
                        "is_adverse": True,
                        "synthetic_walkthrough": True,
                        "scenario": code,
                        "triggered_by": str(actor_id),
                    },
                )
            )
    return tuple(events)
