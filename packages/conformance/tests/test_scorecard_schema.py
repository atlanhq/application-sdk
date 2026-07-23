"""Schema freshness, validation, and rubric-sanity tests for the scorecard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.rubric import load_rubric
from conformance.scorecard.schema import RawTests, TierTestCounts
from conformance.scorecard.validate import (
    _SCHEMA_PATH,
    scorecard_json_schema,
    validate_scorecard,
)

pytest.importorskip("jsonschema")


def test_vendored_schema_is_fresh() -> None:
    """The vendored scorecard-schema-1.0.json must match the model.

    Regenerate with:
        uv run python -c "import json; from conformance.scorecard.validate \\
          import scorecard_json_schema, _SCHEMA_PATH; \\
          _SCHEMA_PATH.write_text(json.dumps(scorecard_json_schema(), indent=2)+chr(10))"
    """
    vendored = json.loads(Path(_SCHEMA_PATH).read_text(encoding="utf-8"))
    assert (
        vendored == scorecard_json_schema()
    ), "scorecard-schema-1.0.json is stale — regenerate it from the model"


def test_built_scorecard_validates() -> None:
    sc = build_scorecard(
        tests=RawTests(
            unit=TierTestCounts(total=10, passed=10),
            integration=TierTestCounts(total=2, passed=2),
            e2e=TierTestCounts(total=1, passed=1),
        ),
        coverage={},
        measured_tiers={"unit", "integration", "e2e"},
        rubric=load_rubric("v1"),
        repo="r",
        app="a",
        commit_sha=None,
        tool_version="0",
        generated_at="t",
    )
    validate_scorecard(sc)  # must not raise
    validate_scorecard(sc.model_dump(by_alias=True, exclude_none=True))


def test_validate_rejects_malformed_document() -> None:
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        validate_scorecard({"repo": "r"})  # missing required fields


def test_rubric_v1_weights_are_coherent() -> None:
    rubric = load_rubric("v1")
    assert rubric.version == "v1"
    # tier weights sum to 1.0
    assert sum(t.weight for t in rubric.tiers) == pytest.approx(1.0)
    # each tier's check weights sum to 1.0
    for tier in rubric.tiers:
        assert sum(c.weight for c in tier.checks) == pytest.approx(1.0), tier.name
    # grade bands include a 0-floor so every score maps to a grade
    assert any(b.min_score == 0 for b in rubric.grade_bands)


def test_rubric_load_is_cached() -> None:
    assert load_rubric("v1") is load_rubric("v1")
