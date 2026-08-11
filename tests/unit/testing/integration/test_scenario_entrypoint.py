"""Tests for declaring which app entrypoint an integration scenario exercises.

Multi-entrypoint apps previously had no way to say this in the Scenario
framework: ``api="workflow"`` only means "POST /start". Which product workflow a
suite covered was recoverable only by reading the test's source, and every
scenario silently hit the app's default entrypoint.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.integration.assertions import equals
from application_sdk.testing.integration.models import Scenario
from application_sdk.testing.integration.runner import BaseIntegrationTest


def _scenario(**kwargs) -> Scenario:
    base = {"name": "wf", "api": "workflow", "assert_that": {"success": equals(True)}}
    base.update(kwargs)
    return Scenario(**base)


class _Suite(BaseIntegrationTest):
    __test__ = False
    scenarios: list[Scenario] = []


def _resolve(suite_cls, scenario: Scenario) -> str:
    return BaseIntegrationTest._resolve_workflow_endpoint(suite_cls, scenario)  # type: ignore[arg-type]


def test_no_entrypoint_declared_keeps_the_bare_endpoint():
    """Single-entrypoint apps must keep working untouched."""
    assert _resolve(_Suite, _scenario()) == "/start"


def test_scenario_entrypoint_is_appended():
    assert _resolve(_Suite, _scenario(entrypoint="miner")) == "/start?entrypoint=miner"


def test_class_level_entrypoint_applies_to_every_scenario():
    class Suite(_Suite):
        __test__ = False
        entrypoint = "crawler"

    assert _resolve(Suite, _scenario()) == "/start?entrypoint=crawler"


def test_scenario_overrides_the_class_default():
    class Suite(_Suite):
        __test__ = False
        entrypoint = "crawler"

    assert _resolve(Suite, _scenario(entrypoint="miner")) == "/start?entrypoint=miner"


def test_explicit_endpoint_still_wins_outright():
    """``endpoint`` is a full override and may carry its own query string."""
    scenario = _scenario(endpoint="/start?entrypoint=custom&foo=1", entrypoint="miner")
    assert _resolve(_Suite, scenario) == "/start?entrypoint=custom&foo=1"


def test_appends_with_ampersand_when_endpoint_already_has_a_query():
    class Suite(_Suite):
        __test__ = False
        workflow_endpoint = "/start?dry_run=true"
        entrypoint = "clean"

    assert _resolve(Suite, _scenario()) == "/start?dry_run=true&entrypoint=clean"


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [("extract-metadata", "extract-metadata"), ("a b", "a%20b"), ("a/b", "a%2Fb")],
)
def test_entrypoint_is_url_encoded(raw, encoded):
    assert _resolve(_Suite, _scenario(entrypoint=raw)) == f"/start?entrypoint={encoded}"


def test_entrypoint_defaults_to_none_on_the_scenario():
    assert _scenario().entrypoint is None


def test_entrypoint_survives_a_full_scenario_construction():
    """Guards the field against being dropped by a future dataclass edit."""
    scenario = Scenario(
        name="crawl",
        api="workflow",
        assert_that={"success": equals(True)},
        entrypoint="crawler",
        schema_base_path="tests/integration/schema/crawler/transformed",
    )
    assert scenario.entrypoint == "crawler"
