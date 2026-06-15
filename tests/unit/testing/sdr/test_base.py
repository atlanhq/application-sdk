"""Tests for `application_sdk.testing.sdr.base.BaseSDRIntegrationTest`."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.testing.integration import Scenario
from application_sdk.testing.sdr import BaseSDRIntegrationTest


@pytest.fixture
def workflow_scenario() -> Scenario:
    return Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
    )


@pytest.fixture
def auth_scenario() -> Scenario:
    return Scenario(name="auth", api="auth", assert_that={"success": lambda v: True})


class _Suite(BaseSDRIntegrationTest):
    agent_spec_template: Dict[str, Any] = {
        "agent-name": "test-agent",
        "secret-path": "test-credentials",
        "auth-type": "basic",
        "basic.username": "username",
        "basic.password": "password",
    }
    scenarios = []  # required so __init_subclass__ does not error


def test_workflow_scenario_gets_agent_routing_args(workflow_scenario: Scenario) -> None:
    suite = _Suite()
    args = suite._build_scenario_args(workflow_scenario)
    assert args["extraction_method"] == "agent"
    assert args["agent_json"] == _Suite.agent_spec_template
    assert (
        "workflow_type" not in args
    ), "workflow_type only injected when subclass sets it"


def test_workflow_scenario_with_workflow_type_set(workflow_scenario: Scenario) -> None:
    class _MultiEntrySuite(_Suite):
        workflow_type = "ecc"
        scenarios = []

    suite = _MultiEntrySuite()
    args = suite._build_scenario_args(workflow_scenario)
    assert args["workflow_type"] == "ecc"


def test_auth_scenario_unchanged(auth_scenario: Scenario) -> None:
    suite = _Suite()
    args = suite._build_scenario_args(auth_scenario)
    assert "extraction_method" not in args
    assert "agent_json" not in args


def test_empty_agent_spec_disables_routing(workflow_scenario: Scenario) -> None:
    class _NoAgentSuite(BaseSDRIntegrationTest):
        agent_spec_template = {}
        scenarios = []

    suite = _NoAgentSuite()
    args = suite._build_scenario_args(workflow_scenario)
    assert "extraction_method" not in args


def test_execute_scenario_polls_workflow_completion(
    workflow_scenario: Scenario,
) -> None:
    suite = _Suite()
    fake_response = {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}

    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(success=True, response=fake_response),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
    ):
        suite._execute_scenario(workflow_scenario)
        ensure.assert_called_once_with(workflow_scenario, fake_response)


def test_execute_scenario_skips_polling_for_auth(auth_scenario: Scenario) -> None:
    suite = _Suite()
    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(success=True, response={"data": {}}),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
    ):
        suite._execute_scenario(auth_scenario)
        ensure.assert_not_called()


def test_execute_scenario_skips_polling_when_expected_data_set(tmp_path) -> None:
    """When expected_data is set the base class already polls — don't double-poll."""
    expected = tmp_path / "expected.json"
    expected.write_text("{}")
    sc = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
        expected_data=str(expected),
    )
    suite = _Suite()
    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(success=True, response={"data": {}}),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
    ):
        suite._execute_scenario(sc)
        ensure.assert_not_called()


def test_require_nonempty_output_runs_floor_check(workflow_scenario: Scenario) -> None:
    """The default non-empty guard calls _validate_output_floor(min_total=1, soft)."""
    suite = _Suite()
    fake_response = {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}
    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(success=True, response=fake_response),
        ),
        patch.object(suite, "_ensure_workflow_completed"),
        patch.object(suite, "_validate_output_floor") as floor,
    ):
        suite._execute_scenario(workflow_scenario)
        floor.assert_called_once()
        kwargs = floor.call_args.kwargs
        assert kwargs.get("min_total") == 1
        assert kwargs.get("soft_if_no_base_path") is True


def test_require_nonempty_output_can_be_disabled(workflow_scenario: Scenario) -> None:
    class _NoCheckSuite(_Suite):
        require_nonempty_output = False
        scenarios = []

    suite = _NoCheckSuite()
    fake_response = {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}
    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(success=True, response=fake_response),
        ),
        patch.object(suite, "_ensure_workflow_completed"),
        patch.object(suite, "_validate_output_floor") as floor,
    ):
        suite._execute_scenario(workflow_scenario)
        floor.assert_not_called()


def test_explicit_floor_not_double_polled() -> None:
    """A scenario with explicit floors is handled by the base runner — the SDR
    layer must not re-poll or re-check it."""
    sc = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
        assert_min_total_assets=5,
    )
    suite = _Suite()
    with (
        patch.object(
            BaseSDRIntegrationTest.__bases__[0],
            "_execute_scenario",
            return_value=MagicMock(
                success=True, response={"data": {"workflow_id": "w", "run_id": "r"}}
            ),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
        patch.object(suite, "_validate_output_floor") as floor,
    ):
        suite._execute_scenario(sc)
        ensure.assert_not_called()
        floor.assert_not_called()
