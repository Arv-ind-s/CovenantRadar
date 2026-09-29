from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import FeatureContribution, FeatureSnapshot, Prediction


def test_feature_snapshot_is_non_identifying_and_content_addressed() -> None:
    snapshot = FeatureSnapshot({"headroom": Decimal("0.2"), "slope": Decimal("-0.01")})

    assert len(snapshot.content_hash) == 64
    assert (
        snapshot.content_hash
        == FeatureSnapshot({"slope": Decimal("-0.01"), "headroom": Decimal("0.2")}).content_hash
    )
    with pytest.raises(ValueError, match="non-identifying"):
        FeatureSnapshot({"borrower_id": Decimal("1")})


def test_prediction_enforces_calibrated_probability_bounds() -> None:
    prediction = Prediction(
        Decimal("0.7"), "stage4:v1", "a" * 64, (FeatureContribution("headroom", Decimal("0.1")),)
    )
    assert prediction.probability == Decimal("0.7")
    with pytest.raises(ValueError, match="between zero and one"):
        Prediction(Decimal("1.1"), "stage4:v1", "a" * 64)


def test_feature_snapshot_cannot_change_after_its_hash_is_recorded() -> None:
    values = {"headroom": Decimal("0.2")}
    snapshot = FeatureSnapshot(values)
    digest = snapshot.content_hash
    values["headroom"] = Decimal("0.9")
    assert snapshot.content_hash == digest
    with pytest.raises(TypeError):
        snapshot.values["headroom"] = Decimal("0.9")


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_model_probability_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        Prediction(Decimal(value), "stage4:v1", "a" * 64)


def test_corrupt_artifact_is_rejected_before_unpickling(tmp_path, monkeypatch) -> None:
    import json

    from covenant_radar.ml import forecast

    artifact = tmp_path / "model.pkl"
    artifact.write_bytes(b"untrusted artifact content")
    artifact.with_suffix(".manifest.json").write_text(json.dumps({"checksum": "a" * 64}))
    monkeypatch.setattr(
        forecast.pickle, "loads", lambda _: pytest.fail("unverified pickle was deserialized")
    )
    with pytest.raises(ValueError, match="checksum"):
        forecast.SklearnForecastPredictor(artifact)


def test_calibrated_ensemble_does_not_claim_first_fold_attribution() -> None:
    from types import SimpleNamespace

    from covenant_radar.ml.forecast import _contributions

    estimator = SimpleNamespace(coef_=[[1.0]])
    model = SimpleNamespace(calibrated_classifiers_=[SimpleNamespace(estimator=estimator)])
    assert _contributions(model, [2.0], ("headroom",)) == ()
