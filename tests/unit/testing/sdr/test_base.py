"""Tests for `application_sdk.testing.sdr.base.BaseSDRIntegrationTest`."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import orjson
import pytest

from application_sdk.testing.integration import Scenario
from application_sdk.testing.sdr import BaseSDRIntegrationTest


def _write_manifest(tmp_path, extract_args: Dict[str, Any]) -> str:
    """Write a minimal AE manifest.json whose extract node carries ``extract_args``."""
    manifest = {
        "execution_mode": "automation-engine",
        "dag": {
            "extract": {
                "inputs": {
                    "workflow_type": "MSSQLMetadataExtractionWorkflow",
                    "args": extract_args,
                }
            }
        },
    }
    path = tmp_path / "manifest.json"
    path.write_bytes(orjson.dumps(manifest))
    return str(path)


# Extract args shaped like a real manifest, WITH the agent_json slot (post-#177).
_ARGS_WITH_AGENT_JSON: Dict[str, Any] = {
    "connection": "{{connection}}",
    "credential_guid": "{{credential-guid}}",
    "extraction_method": "{{extraction-method}}",
    "agent_json": "{{agent-json}}",
    "include_filter": "{{include-filter}}",
    "exclude_filter": "{{exclude-filter}}",
    "preflight_check": "{{preflight-check}}",
}

# Same, but MISSING the agent_json slot — the atlan-mssql-app#177 bug.
_ARGS_MISSING_AGENT_JSON: Dict[str, Any] = {
    "connection": "{{connection}}",
    "credential_guid": "{{credential-guid}}",
    "extraction_method": "{{extraction-method}}",
    "include_filter": "{{include-filter}}",
    "exclude_filter": "{{exclude-filter}}",
}


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


def test_manifest_driven_args_built_from_extract_node(
    tmp_path, workflow_scenario: Scenario
) -> None:
    """With manifest_path set, workflow input is derived from the manifest's
    extract args (substituted), not hand-written."""

    class _ManifestSuite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, dict(_ARGS_WITH_AGENT_JSON))
        agent_spec_template = {
            "agent-name": "mssql-ci-agent",
            "secret-path": "mssql-credentials",
            "auth-type": "basic",
        }
        default_connection = {
            "typeName": "Connection",
            "attributes": {"qualifiedName": "default/mssql/1700000000"},
        }
        scenarios = []

    args = _ManifestSuite()._build_scenario_args(workflow_scenario)
    # Manifest declared the slot → agent_json is substituted in.
    assert args["agent_json"] == _ManifestSuite.agent_spec_template
    assert args["extraction_method"] == "agent"
    assert args["connection"] == _ManifestSuite.default_connection
    assert args["preflight_check"] is True
    # The manifest's sibling inputs.workflow_type (the AE workflow-type slug) must
    # NOT leak into the start body — it's not an SDK entrypoint name and the start
    # handler would 400 on it. Entrypoint stays class-controlled (unset here).
    assert "workflow_type" not in args


def test_manifest_workflow_type_is_class_controlled_not_from_manifest(
    tmp_path, workflow_scenario: Scenario
) -> None:
    """Only self.workflow_type (an SDK entrypoint name) sets the start-body
    workflow_type; the manifest's AE slug is never used."""

    class _EntrypointSuite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, dict(_ARGS_WITH_AGENT_JSON))
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        workflow_type = "ecc"  # an SDK entrypoint name, not the AE slug
        scenarios = []

    args = _EntrypointSuite()._build_scenario_args(workflow_scenario)
    assert args["workflow_type"] == "ecc"


def test_manifest_filters_come_from_per_scenario_metadata(tmp_path) -> None:
    """Connector-specific filters are supplied per-scenario via metadata (the base
    does NOT enumerate them); a placeholder no scenario covers defaults to "" —
    never a literal {{...}}."""

    class _FilterSuite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, dict(_ARGS_WITH_AGENT_JSON))
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        scenarios = []

    with_meta = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
        metadata={"include-filter": '{"^db$":[]}', "exclude-filter": "{}"},
    )
    args = _FilterSuite()._build_scenario_args(with_meta)
    assert args["include_filter"] == '{"^db$":[]}'  # from scenario metadata
    assert args["exclude_filter"] == "{}"

    # No metadata for the filter → defaults to "" (extract-everything), and the
    # placeholder is not left as a literal "{{include-filter}}".
    without_meta = Scenario(
        name="wf2",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
    )
    args2 = _FilterSuite()._build_scenario_args(without_meta)
    assert args2["include_filter"] == ""
    assert args2["exclude_filter"] == ""


def test_manifest_warns_on_unmatched_metadata_key_and_uncovered_placeholder(
    tmp_path,
) -> None:
    """A metadata key matching no placeholder (hyphen vs underscore) and a
    placeholder no value covers are both defaulted to "" but WARNED — so a
    missing required input / misspelt metadata key is never silently masked."""
    extract_args = {
        "agent_json": "{{agent-json}}",
        "connection": "{{connection}}",
        "include_filter": "{{include_filter}}",  # underscore — won't match hyphen metadata
        "target": "{{target}}",  # non-filter slot, no value anywhere
    }

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, extract_args)
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        scenarios = []

    sc = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
        metadata={
            "include-filter": '{"^db$":[]}'
        },  # hyphen — mismatches {{include_filter}}
    )
    with patch("application_sdk.testing.sdr.base.logger") as mock_logger:
        args = _Suite()._build_scenario_args(sc)

    # Unresolved placeholders defaulted to "" — no literal {{...}} leaks through.
    assert args["include_filter"] == ""
    assert args["target"] == ""
    # ...but both problems were warned, not silently masked.
    warned = " ".join(str(c.args) for c in mock_logger.warning.call_args_list)
    assert "matches no" in warned  # the hyphen-vs-underscore metadata mismatch
    assert "{{target}}" in warned  # the uncovered non-filter slot


def test_manifest_missing_agent_json_slot_is_not_injected(
    tmp_path, workflow_scenario: Scenario
) -> None:
    """The whole point (atlan-mssql-app#177): a manifest with no agent_json slot
    must produce a workflow input WITHOUT agent_json — so the agent run fails and
    the e2e surfaces the gap — unlike the legacy path that injected it regardless."""

    class _BadManifestSuite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, dict(_ARGS_MISSING_AGENT_JSON))
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        scenarios = []

    args = _BadManifestSuite()._build_scenario_args(workflow_scenario)
    assert (
        "agent_json" not in args
    ), "manifest had no agent_json slot — the derived input must not contain it"
    # Slots the manifest DID declare are still substituted.
    assert args["extraction_method"] == "agent"


def test_manifest_missing_file_raises(workflow_scenario: Scenario) -> None:
    class _MissingManifestSuite(BaseSDRIntegrationTest):
        manifest_path = "/nonexistent/manifest.json"
        agent_spec_template = {"agent-name": "a"}
        scenarios = []

    with pytest.raises(FileNotFoundError, match="SDR manifest not found"):
        _MissingManifestSuite()._build_scenario_args(workflow_scenario)


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
