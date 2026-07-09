"""Tests for `application_sdk.testing.sdr.base.BaseSDRIntegrationTest`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import orjson
import pytest

from application_sdk.testing.integration import BaseIntegrationTest, Scenario
from application_sdk.testing.sdr import BaseSDRIntegrationTest


def _write_manifest(tmp_path, extract_args: dict[str, Any]) -> str:
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
_ARGS_WITH_AGENT_JSON: dict[str, Any] = {
    "connection": "{{connection}}",
    "credential_guid": "{{credential-guid}}",
    "extraction_method": "{{extraction-method}}",
    "agent_json": "{{agent-json}}",
    "include_filter": "{{include-filter}}",
    "exclude_filter": "{{exclude-filter}}",
    "preflight_check": "{{preflight-check}}",
}

# Same, but MISSING the agent_json slot — the atlan-mssql-app#177 bug.
_ARGS_MISSING_AGENT_JSON: dict[str, Any] = {
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
    agent_spec_template: dict[str, Any] = {
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


def test_manifest_metadata_collision_with_universal_slot_warns(tmp_path) -> None:
    """A scenario metadata key colliding with a reserved universal SDR slot (e.g.
    ``connection``) is ignored — the universal value wins — and the silent drop is
    WARNED rather than masked."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(
            tmp_path,
            {"connection": "{{connection}}", "agent_json": "{{agent-json}}"},
        )
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        default_connection = {
            "typeName": "Connection",
            "attributes": {"qualifiedName": "default/x/1"},
        }
        scenarios = []

    sc = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
        metadata={"connection": "SHOULD-BE-IGNORED"},
    )
    with patch("application_sdk.testing.sdr.base.logger") as mock_logger:
        args = _Suite()._build_scenario_args(sc)

    # The universal value wins; the colliding metadata is NOT substituted.
    assert args["connection"] == _Suite.default_connection
    warned = " ".join(str(c.args) for c in mock_logger.warning.call_args_list)
    assert "collides with a reserved universal SDR slot" in warned


def test_manifest_without_agent_spec_warns_and_falls_back_to_direct(tmp_path) -> None:
    """manifest_path set + empty agent_spec_template → extraction-method falls back
    to 'direct' (no agent spec injected) — warned, not silent."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(
            tmp_path,
            {
                "extraction_method": "{{extraction-method}}",
                "agent_json": "{{agent-json}}",
            },
        )
        agent_spec_template = {}  # empty → degrades to direct
        scenarios = []

    sc = Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
    )
    with patch("application_sdk.testing.sdr.base.logger") as mock_logger:
        args = _Suite()._build_scenario_args(sc)

    assert args["extraction_method"] == "direct"
    warned = " ".join(str(c.args) for c in mock_logger.warning.call_args_list)
    assert "falls back to 'direct'" in warned


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


def test_manifest_without_extract_args_raises(
    tmp_path, workflow_scenario: Scenario
) -> None:
    """A manifest whose extract node has no ``args`` object raises ValueError
    (rather than silently deriving an empty input). Pins the second raise branch
    of ``_manifest_extract_inputs`` (the first, FileNotFoundError, is covered above).
    """
    manifest = {
        "execution_mode": "automation-engine",
        "dag": {"extract": {"inputs": {"workflow_type": "X"}}},  # no `args`
    }
    path = tmp_path / "manifest.json"
    path.write_bytes(orjson.dumps(manifest))

    class _NoArgsManifestSuite(BaseSDRIntegrationTest):
        manifest_path = str(path)
        agent_spec_template = {"agent-name": "a"}
        scenarios = []

    with pytest.raises(ValueError, match="dag.extract.inputs.args"):
        _NoArgsManifestSuite()._build_scenario_args(workflow_scenario)


def test_execute_scenario_polls_workflow_completion(
    workflow_scenario: Scenario,
) -> None:
    suite = _Suite()
    fake_response = {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}

    with (
        patch.object(
            BaseIntegrationTest,
            "_execute_scenario",
            return_value=MagicMock(success=True, response=fake_response),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
    ):
        suite._execute_scenario(workflow_scenario)
        ensure.assert_called_once_with(workflow_scenario, fake_response)


def test_execute_scenario_mutates_result_on_guard_exception(
    workflow_scenario: Scenario,
) -> None:
    """On an exception while polling completion / running a guard, `_execute_scenario`
    mutates the SAME `ScenarioResult` the parent already appended to `cls._results`
    (with `success=True`) to `success=False` + `error=exc` before re-raising — so
    the on-disk summary reflects the real, failed outcome instead of a false green
    on a scenario pytest reports as FAILED."""
    suite = _Suite()
    fake_response = {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}
    fake_result = MagicMock(success=True, response=fake_response, error=None)
    boom = RuntimeError("workflow never completed")

    with (
        patch.object(
            BaseIntegrationTest,
            "_execute_scenario",
            return_value=fake_result,
        ),
        patch.object(suite, "_ensure_workflow_completed", side_effect=boom),
    ):
        with pytest.raises(RuntimeError, match="workflow never completed"):
            suite._execute_scenario(workflow_scenario)

    assert fake_result.success is False
    assert fake_result.error is boom


def test_execute_scenario_skips_polling_for_auth(auth_scenario: Scenario) -> None:
    suite = _Suite()
    with (
        patch.object(
            BaseIntegrationTest,
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
            BaseIntegrationTest,
            "_execute_scenario",
            return_value=MagicMock(success=True, response={"data": {}}),
        ),
        patch.object(suite, "_ensure_workflow_completed") as ensure,
    ):
        suite._execute_scenario(sc)
        ensure.assert_not_called()


# --------------------------------------------------------------------------- #
# Live-manifest source (BLDX-1493) — prefer the running app's /manifest
# --------------------------------------------------------------------------- #


def _committed_manifest_args() -> dict[str, Any]:
    """Args distinct from the live manifest so we can tell the sources apart."""
    return {"connection": "{{connection}}", "committed_marker": "from-file"}


def test_load_manifest_prefers_live_endpoint(tmp_path) -> None:
    """When the running app serves a manifest, it wins over the committed file."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        scenarios = []

    suite = _Suite()
    live = {"dag": {"extract": {"inputs": {"args": {"live_marker": "from-endpoint"}}}}}
    suite.client = MagicMock()
    suite.client.get_manifest.return_value = live

    args = suite._manifest_extract_inputs()
    assert args == {"live_marker": "from-endpoint"}
    # Entrypoint (workflow_type) forwarded to the endpoint; None here.
    suite.client.get_manifest.assert_called_once_with(entrypoint=None)


def test_load_manifest_forwards_workflow_type_as_entrypoint(tmp_path) -> None:
    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        workflow_type = "s4"
        scenarios = []

    suite = _Suite()
    suite.client = MagicMock()
    suite.client.get_manifest.return_value = {
        "dag": {"extract": {"inputs": {"args": {"x": 1}}}}
    }
    suite._manifest_extract_inputs()
    suite.client.get_manifest.assert_called_once_with(entrypoint="s4")


def test_load_manifest_falls_back_to_file_when_live_unreachable(tmp_path) -> None:
    """A dead endpoint (get_manifest -> None) falls back to the committed file."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        scenarios = []

    suite = _Suite()
    suite.client = MagicMock()
    suite.client.get_manifest.return_value = None

    args = suite._manifest_extract_inputs()
    assert args["committed_marker"] == "from-file"


def test_load_manifest_reads_file_when_no_client(tmp_path) -> None:
    """No client attached (e.g. pure unit context) → committed file is used."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        scenarios = []

    args = _Suite()._manifest_extract_inputs()
    assert args["committed_marker"] == "from-file"


def test_sdk_level_unreachable_raises(tmp_path, monkeypatch) -> None:
    """SDK-level run (ATLAN_E2E_REQUIRE_LIVE_MANIFEST=true): an unreachable
    /manifest fails hard rather than silently reading the stale committed file."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        scenarios = []

    suite = _Suite()
    suite.client = MagicMock()
    suite.client.get_manifest.return_value = None
    monkeypatch.setenv("ATLAN_E2E_REQUIRE_LIVE_MANIFEST", "true")
    with pytest.raises(RuntimeError, match="ATLAN_E2E_REQUIRE_LIVE_MANIFEST"):
        suite._manifest_extract_inputs()


def test_sdk_level_no_client_raises(tmp_path, monkeypatch) -> None:
    """SDK-level run without a live-capable client must also fail hard — a
    missing client is just another way of never exercising the live manifest
    (BLDX-1493 false-green guard)."""

    class _Suite(BaseSDRIntegrationTest):
        manifest_path = _write_manifest(tmp_path, _committed_manifest_args())
        scenarios = []

    monkeypatch.setenv("ATLAN_E2E_REQUIRE_LIVE_MANIFEST", "true")
    with pytest.raises(RuntimeError, match="ATLAN_E2E_REQUIRE_LIVE_MANIFEST"):
        _Suite()._manifest_extract_inputs()


# ---------------------------------------------------------------------------
# SDR readiness — dynamic assertions: assets landed (count + location) + floor
# ---------------------------------------------------------------------------

_RESP = {"data": {"workflow_id": "wf1", "run_id": "run1"}}
_LOAD = "application_sdk.testing.integration.comparison.load_actual_output"


def _asset(qn: str) -> dict[str, Any]:
    return {"typeName": "Table", "attributes": {"qualifiedName": qn}}


class _WfSuite(BaseSDRIntegrationTest):
    agent_spec_template = {"agent-name": "a", "secret-path": "p"}
    extracted_output_base_path = "data/out"
    default_connection = {
        "typeName": "Connection",
        "attributes": {"qualifiedName": "default/mssql/1700"},
    }
    scenarios = []


def test_assets_landed_passes_when_records_under_connection(
    workflow_scenario: Scenario,
) -> None:
    suite = _WfSuite()
    with patch(
        _LOAD,
        return_value=[
            _asset("default/mssql/1700/db/sch/t1"),
            _asset("default/mssql/1700/db/sch/t2"),
        ],
    ):
        suite._assert_assets_landed(workflow_scenario, _RESP)  # no raise


def test_assets_landed_fails_on_zero_assets_missing_dir(
    workflow_scenario: Scenario,
) -> None:
    suite = _WfSuite()
    with patch(_LOAD, side_effect=FileNotFoundError("no output dir")):
        with pytest.raises(AssertionError, match="NO extracted assets"):
            suite._assert_assets_landed(workflow_scenario, _RESP)


def test_assets_landed_fails_on_empty_record_list(
    workflow_scenario: Scenario,
) -> None:
    suite = _WfSuite()
    with patch(_LOAD, return_value=[]):
        with pytest.raises(AssertionError, match="ZERO asset records"):
            suite._assert_assets_landed(workflow_scenario, _RESP)


def test_assets_landed_fails_on_wrong_location(workflow_scenario: Scenario) -> None:
    suite = _WfSuite()
    with patch(
        _LOAD,
        return_value=[
            _asset("default/mssql/1700/db/sch/t1"),
            _asset("default/OTHER/9999/db/sch/t2"),  # not under the connection QN
        ],
    ):
        with pytest.raises(AssertionError, match="NOT nested under the connection"):
            suite._assert_assets_landed(workflow_scenario, _RESP)


def test_assets_landed_flags_sibling_prefix_boundary(
    workflow_scenario: Scenario,
) -> None:
    """A sibling connection that shares a numeric prefix (…/1700 vs …/17000)
    must be flagged misplaced — the connection-prefix check is segment-boundary
    aware, not a bare startswith."""
    suite = _WfSuite()
    with patch(
        _LOAD,
        return_value=[
            _asset("default/mssql/1700/db/sch/t1"),
            _asset(
                "default/mssql/17000/db/sch/t2"
            ),  # different connection, shared prefix
        ],
    ):
        with pytest.raises(AssertionError, match="NOT nested under the connection"):
            suite._assert_assets_landed(workflow_scenario, _RESP)


def test_assets_landed_allows_connection_asset_itself(
    workflow_scenario: Scenario,
) -> None:
    """The connection asset itself (qn == connection QN) is nested, not misplaced."""
    suite = _WfSuite()
    with patch(
        _LOAD,
        return_value=[
            _asset("default/mssql/1700"),  # the connection asset itself
            _asset("default/mssql/1700/db/sch/t1"),
        ],
    ):
        suite._assert_assets_landed(workflow_scenario, _RESP)  # no raise


def test_execute_scenario_warns_when_guard_enabled_but_expected_data_set(
    tmp_path, monkeypatch
) -> None:
    """A guard enabled via env is a silent no-op when the scenario validates via
    expected_data — the guard must NOT run and the skip must be warned, so a
    green tick isn't mistaken for assets-landed coverage."""
    monkeypatch.setenv("SDR_REQUIRE_ASSETS_LANDED", "true")
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
            BaseIntegrationTest,
            "_execute_scenario",
            return_value=MagicMock(success=True, response=_RESP),
        ),
        patch.object(suite, "_assert_assets_landed") as guard,
        patch("application_sdk.testing.sdr.base.logger") as mock_logger,
    ):
        suite._execute_scenario(sc)
    guard.assert_not_called()
    warned = " ".join(str(c.args) for c in mock_logger.warning.call_args_list)
    assert "guard is enabled" in warned


def test_assets_landed_skips_location_when_conn_qn_unresolved(
    workflow_scenario: Scenario,
) -> None:
    class _NoConn(_WfSuite):
        default_connection = {}
        scenarios = []

    with patch(_LOAD, return_value=[_asset("anything/at/all")]):
        _NoConn()._assert_assets_landed(
            workflow_scenario, _RESP
        )  # count ok, loc skipped


def test_assets_landed_warns_and_skips_when_no_base_path(
    workflow_scenario: Scenario,
) -> None:
    class _NoBase(BaseSDRIntegrationTest):
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        extracted_output_base_path = ""
        scenarios = []

    with patch(_LOAD) as m:
        _NoBase()._assert_assets_landed(workflow_scenario, _RESP)
        m.assert_not_called()  # no base path → can't locate output → warn + return


def test_assets_landed_fails_cleanly_on_missing_ids(
    workflow_scenario: Scenario,
) -> None:
    """A COMPLETED response missing workflow_id/run_id raises the diagnostic
    AssertionError, not a TypeError from os.path.join(base, None)."""
    suite = _WfSuite()
    with patch(_LOAD) as m:
        with pytest.raises(AssertionError, match="missing workflow_id/run_id"):
            suite._assert_assets_landed(workflow_scenario, {"data": {}})
        m.assert_not_called()  # bail before trying to locate output


def test_sdr_connection_qn_nested_flat_and_unresolved() -> None:
    class _N(BaseSDRIntegrationTest):
        default_connection = {"attributes": {"qualifiedName": "default/x/1"}}
        scenarios = []

    class _F(BaseSDRIntegrationTest):
        default_connection = {"connection_qualified_name": "default/y/2"}
        scenarios = []

    class _U(BaseSDRIntegrationTest):
        default_connection = {}
        scenarios = []

    assert _N()._sdr_connection_qn() == "default/x/1"
    assert _F()._sdr_connection_qn() == "default/y/2"
    assert _U()._sdr_connection_qn() is None


def _wf_scen() -> Scenario:
    return Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        workflow_timeout=300,
    )


def test_floor_skips_non_agent_suite() -> None:
    class _Direct(BaseSDRIntegrationTest):
        agent_spec_template = {}
        scenarios = []

    with pytest.raises(pytest.skip.Exception):
        _Direct().test_sdr_suite_runs_an_extraction()


def test_floor_passes_when_workflow_scenario_present() -> None:
    class _Ok(BaseSDRIntegrationTest):
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        scenarios = [_wf_scen()]

    _Ok().test_sdr_suite_runs_an_extraction()  # returns (no skip, no raise)


def test_floor_advisory_skip_when_auth_only_and_not_enforced() -> None:
    class _AuthOnly(BaseSDRIntegrationTest):
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        scenarios = [
            Scenario(name="auth", api="auth", assert_that={"success": lambda v: True})
        ]

    with pytest.raises(pytest.skip.Exception):
        _AuthOnly().test_sdr_suite_runs_an_extraction()


def test_floor_hard_fails_when_auth_only_and_enforced() -> None:
    class _Enforced(BaseSDRIntegrationTest):
        agent_spec_template = {"agent-name": "a", "secret-path": "p"}
        enforce_workflow_floor = True
        scenarios = [
            Scenario(name="auth", api="auth", assert_that={"success": lambda v: True})
        ]

    with pytest.raises(AssertionError, match="no api='workflow' scenario"):
        _Enforced().test_sdr_suite_runs_an_extraction()


# ---------------------------------------------------------------------------
# Upstream (atlan) object-store assertion — the Looker-class catch
# ---------------------------------------------------------------------------


class _UpstreamSuite(_WfSuite):
    require_upstream_assets_landed = True
    upstream_output_base_path = "data-upstream/artifacts/apps/x/workflows"
    scenarios = []


def test_upstream_assets_landed_passes_when_upstream_populated(
    workflow_scenario: Scenario,
) -> None:
    suite = _UpstreamSuite()
    with patch(_LOAD, return_value=[_asset("default/mssql/1700/db/sch/t1")]):
        suite._assert_upstream_assets_landed(workflow_scenario, _RESP)  # no raise


def test_upstream_assets_landed_fails_when_upstream_empty(
    workflow_scenario: Scenario,
) -> None:
    """Deployment store has assets, but the upstream transformed/ is empty
    (missing upload_to_atlan) → Publish would publish 0 → hard failure."""
    suite = _UpstreamSuite()
    with patch(_LOAD, side_effect=FileNotFoundError("no upstream transformed/")):
        with pytest.raises(AssertionError, match="would publish ZERO assets"):
            suite._assert_upstream_assets_landed(workflow_scenario, _RESP)


def test_upstream_assets_landed_fails_on_zero_records(
    workflow_scenario: Scenario,
) -> None:
    suite = _UpstreamSuite()
    with patch(_LOAD, return_value=[]):
        with pytest.raises(AssertionError, match="ZERO records"):
            suite._assert_upstream_assets_landed(workflow_scenario, _RESP)


def test_upstream_assets_landed_warns_when_no_upstream_path(
    workflow_scenario: Scenario,
) -> None:
    class _NoUpstreamPath(_WfSuite):
        require_upstream_assets_landed = True
        upstream_output_base_path = None
        scenarios = []

    with patch(_LOAD) as m:
        _NoUpstreamPath()._assert_upstream_assets_landed(workflow_scenario, _RESP)
        m.assert_not_called()  # no upstream path → warn + return, can't verify


def test_upstream_assets_landed_fails_cleanly_on_missing_ids(
    workflow_scenario: Scenario,
) -> None:
    """Mirrors test_assets_landed_fails_cleanly_on_missing_ids for the upstream
    guard: a COMPLETED response missing workflow_id/run_id raises the diagnostic
    AssertionError, not a TypeError from os.path.join(base, None) downstream."""
    suite = _UpstreamSuite()
    with patch(_LOAD) as m:
        with pytest.raises(AssertionError, match="missing workflow_id/run_id"):
            suite._assert_upstream_assets_landed(workflow_scenario, {"data": {}})
        m.assert_not_called()  # bail before trying to locate output


# ---------------------------------------------------------------------------
# Env-controllable guards — lets the sdr-e2e action drive the harness SDK-side
# (enable a guard + point it at the store mount) without editing the connector.
# ---------------------------------------------------------------------------


def test_sdr_flag_env_overrides_classvar(monkeypatch) -> None:
    f = BaseSDRIntegrationTest._sdr_flag
    monkeypatch.setenv("SDR_REQUIRE_ASSETS_LANDED", "true")
    assert f("SDR_REQUIRE_ASSETS_LANDED", False) is True  # env True beats default False
    monkeypatch.setenv("SDR_REQUIRE_ASSETS_LANDED", "false")
    assert f("SDR_REQUIRE_ASSETS_LANDED", True) is False  # env False beats default True
    monkeypatch.delenv("SDR_REQUIRE_ASSETS_LANDED", raising=False)
    assert f("SDR_REQUIRE_ASSETS_LANDED", True) is True  # env unset → class default


def test_upstream_base_path_read_from_env(
    monkeypatch, workflow_scenario: Scenario
) -> None:
    """The action can point the upstream guard at the mount it created via env,
    with no upstream_output_base_path set on the connector's suite."""
    monkeypatch.setenv("SDR_UPSTREAM_OUTPUT_BASE_PATH", "data-upstream/out")

    class _EnvUpstream(_WfSuite):
        require_upstream_assets_landed = True
        upstream_output_base_path = None  # not set on the class — env supplies it
        scenarios = []

    with patch(_LOAD, return_value=[_asset("default/mssql/1700/db/t")]) as m:
        _EnvUpstream()._assert_upstream_assets_landed(workflow_scenario, _RESP)
        assert m.call_args.args[0] == "data-upstream/out"  # env path was used
