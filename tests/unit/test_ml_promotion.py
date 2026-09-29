"""An approval must bind the exact bytes loaded by the nightly runtime."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from covenant_radar.ml.forecast import SklearnForecastPredictor
from covenant_radar.services import nightly_runtime


@pytest.mark.parametrize(
    "approved,registered_checksum,version,expected",
    [
        (True, "a" * 64, "customer-v1", "champion"),
        (True, "b" * 64, "customer-v1", "shadow"),
        (False, "a" * 64, "customer-v1", "shadow"),
        (True, "a" * 64, "synthetic-reference-v1:logistic", "shadow"),
    ],
)
def test_promotion_requires_exact_approved_artifact(
    monkeypatch, approved, registered_checksum, version, expected
) -> None:
    predictor = object.__new__(SklearnForecastPredictor)
    predictor.version = version
    predictor.checksum = "a" * 64
    record = SimpleNamespace(
        is_approved=approved,
        provider="scikit-learn",
        model_id=f"sha256:{registered_checksum}",
        state="approved" if approved else "registered",
    )
    repository = Mock()
    repository.get_by_component.return_value = record
    monkeypatch.setattr(nightly_runtime, "SqlAlchemyModelRegistryRepository", lambda _: repository)
    settings = SimpleNamespace(forecast=SimpleNamespace(ml_mode="champion"))
    factory = Mock()
    assert nightly_runtime._predictor_mode(settings, factory, predictor) == expected
