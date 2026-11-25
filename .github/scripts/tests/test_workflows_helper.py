"""The shared workflow helpers fail loudly instead of returning nothing."""

from __future__ import annotations

from typing import Any

import pytest
from _workflows import job, step_named, steps, triggers

_STEP = {"name": "Checkout", "uses": "actions/checkout@v4"}


@pytest.mark.parametrize(
    "doc",
    [
        {"runs-on": "ubuntu-latest"},
        {"runs-on": "ubuntu-latest", "steps": None},
        {"runs-on": "ubuntu-latest", "steps": []},
        {"uses": "./.github/workflows/reusable.yaml"},
        {"runs": {"using": "composite"}},
        {"runs": {"using": "composite", "steps": []}},
    ],
    ids=["no-key", "null", "empty", "reusable-call", "action-no-key", "action-empty"],
)
def test_steps_refuses_a_job_or_action_without_steps(doc: dict) -> None:
    """A "no step does X" guard over [] would pass with nothing checked."""
    with pytest.raises(AssertionError, match="non-empty steps"):
        steps(doc)


def test_steps_reads_a_job_and_a_composite_action() -> None:
    assert steps({"steps": [_STEP]}) == [_STEP]
    assert steps({"runs": {"using": "composite", "steps": [_STEP]}}) == [_STEP]


def test_step_named_requires_exactly_one_match() -> None:
    assert step_named({"steps": [_STEP]}, "Checkout") == _STEP
    with pytest.raises(AssertionError, match="found 2"):
        step_named({"steps": [_STEP, _STEP]}, "Checkout")
    with pytest.raises(AssertionError, match="found 0"):
        step_named({"steps": [_STEP]}, "Build")


def test_job_names_the_missing_job() -> None:
    with pytest.raises(AssertionError, match="no job 'build'"):
        job({"jobs": {"test": {"steps": [_STEP]}}}, "build")


def test_triggers_reads_the_key_pyyaml_turns_into_true() -> None:
    as_parsed: dict[Any, Any] = {True: {"push": None}}
    assert triggers(as_parsed) == {"push": None}
    assert triggers({"on": {"push": None}}) == {"push": None}
