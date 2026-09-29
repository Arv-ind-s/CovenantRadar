"""Cushion, breakeven and status logic for market pressure on interest cover."""

from datetime import date
from uuid import uuid4

import pytest

from covenant_radar.services.borrower_market import (
    BorrowerPosition,
    assess,
    quarter_label,
    sort_key,
)
from tests.unit.test_market_intelligence import fixture_snapshot


def _position(industry="H51", *, ebit=16.2, revenue=380.0, finance=10.0, debt=128.0, **extra):
    values = {
        "borrower_id": uuid4(),
        "reference": "B-1",
        "name": "Example Aviation Private Limited",
        "industry_code": industry,
        "fy_label": "FY26Q2",
        "period_start": date(2026, 4, 1),
        "period_end": date(2026, 6, 30),
        "revenue": revenue,
        "ebit": ebit,
        "finance_cost": finance,
        "total_debt": debt,
        "threshold": 1.5,
        "covenant_name": "Interest coverage ratio",
    }
    values.update(extra)
    return BorrowerPosition(**values)


def _snapshot(**series):
    return {
        "series": {key: {"observations": points} for key, points in series.items()},
        "news": {},
    }


def _rising_brent():
    # Reported quarter averages 100; the quarter so far averages 97.5; latest 115.
    return [["2026-05-01", 100.0], ["2026-07-15", 80.0], ["2026-09-22", 115.0]]


def test_cushion_and_rate_tolerance_come_from_reported_figures():
    result = assess(_position(), _snapshot())
    assert result.icr == pytest.approx(1.62)
    assert result.cushion == pytest.approx(16.2 - 15)
    assert result.cushion_pct == pytest.approx(1.2 / 16.2 * 100)
    # Rates can rise until EBIT / (finance + debt * dr / 4) == 1.5.
    tolerance = result.rate_tolerance_bp / 10_000
    assert 16.2 / (10 + 128 * tolerance / 4) == pytest.approx(1.5)


def test_fuel_spike_at_latest_prices_exceeds_a_thin_cushion():
    result = assess(_position(), _snapshot(DCOILBRENTEU=_rising_brent()))
    assert result.status == "exceeds" and result.priority == "now"
    lead = result.lead
    assert lead.key == "inputs" and lead.lens == "latest"
    assert lead.breakeven == pytest.approx(1.2 / (380 * 0.30) * 100)
    assert lead.latest == pytest.approx(15)
    assert lead.qtd < 0
    assert "Dec 2026 quarter" in result.implication
    assert "Sep 2026 quarter test should benefit" in result.implication
    assert "15.0%" in result.headline and "breakeven" in result.headline


def test_offsetting_inputs_are_netted_in_the_basket():
    # Auto components: iron ore and aluminium fall more than rubber rises.
    snapshot = _snapshot(
        PIORECRUSDM=[["2026-05-01", 100.0], ["2026-07-01", 90.0]],
        PALUMUSDM=[["2026-05-01", 100.0], ["2026-07-01", 90.0]],
        PRUBBUSDM=[["2026-05-01", 100.0], ["2026-07-01", 110.0]],
    )
    result = assess(_position("C29", ebit=15.4, revenue=433.0), snapshot)
    inputs = next(channel for channel in result.channels if channel.key == "inputs")
    assert inputs.qtd == pytest.approx((0.20 * -10 + 0.12 * -10 + 0.05 * 10) / 0.37)
    assert inputs.status == "easing"
    assert result.status == "easing"
    assert "cushion is thin" in result.implication


def test_below_minimum_has_no_tolerance_and_ranks_first():
    below = assess(_position("C13", ebit=10.8, revenue=433.0), _snapshot())
    assert below.status == "below"
    assert below.rate_tolerance_bp is None
    assert "already below" in below.headline
    fine = assess(_position(ebit=40.0), _snapshot(DCOILBRENTEU=_rising_brent()))
    assert sorted([fine, below], key=sort_key)[0] is below


def test_rate_rise_beyond_tolerance_is_flagged():
    snapshot = _snapshot(IRSTCI01INM156N=[["2026-05-01", 5.5], ["2026-08-01", 7.0]])
    result = assess(_position("E36", ebit=15.5), snapshot)
    rates = next(channel for channel in result.channels if channel.key == "rates")
    assert rates.latest == pytest.approx(150)
    assert rates.breakeven < 150
    assert result.status == "exceeds"
    assert "floating-rate" in result.implication


def test_exporters_read_a_weaker_rupee_as_support_and_a_stronger_one_as_adverse():
    weaker = _snapshot(DEXINUS=[["2026-05-01", 90.0], ["2026-08-01", 93.0]])
    assert assess(_position("J62", ebit=30.0), weaker).status == "easing"
    stronger = _snapshot(DEXINUS=[["2026-05-01", 90.0], ["2026-08-01", 86.0]])
    result = assess(_position("J62", ebit=30.0), stronger)
    assert result.status == "watch"
    assert result.lead.status == "adverse"


def test_unmapped_sector_with_flat_rates_adds_no_signal():
    snapshot = _snapshot(IRSTCI01INM156N=[["2026-05-01", 5.5], ["2026-08-01", 5.5]])
    result = assess(_position("G47", ebit=40.0), snapshot)
    assert result.status == "unexposed" and result.priority == "none"
    assert "no signal" in result.implication


def test_missing_market_data_is_not_reported_as_no_exposure():
    result = assess(_position(), _snapshot())
    assert result.status == "no_market"
    assert result.priority == "none"
    assert "unavailable" in result.label


def test_missing_statements_or_covenant_is_no_data():
    assert assess(_position(ebit=None), _snapshot()).status == "no_data"
    assert assess(_position(threshold=None), _snapshot()).status == "no_data"
    assert assess(_position(period_end=None), _snapshot()).status == "no_data"


def test_quarter_labels_roll_over_the_year():
    assert quarter_label(date(2026, 6, 30), 1) == "Sep 2026 quarter"
    assert quarter_label(date(2026, 9, 30), 2) == "Mar 2027 quarter"
    assert quarter_label(None, 1) == ""


def test_real_markets_put_a_thin_aviation_cushion_under_pressure(tmp_path):
    snapshot = fixture_snapshot(tmp_path)
    result = assess(_position(), snapshot)
    assert result.status == "exceeds"
    assert result.lead.latest_date == "2026-09-22"
    assert result.news and all(story["publisher"] for story in result.news)
    comfortable = assess(_position("C24", ebit=40.0), snapshot)
    assert comfortable.status == "easing"
