"""Unit tests for BaseE2ETest — manifest loader and substitution walker.

Covers the two methods the review flagged as uncovered (TEST-001/G4):
  - ``_apply_mustache_subs``: recursive {{...}} replacement
  - ``_seed_dag_from_manifest``: manifest JSON loading + queue patching + subs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e._errors import (
    HarnessMethodNotImplementedError,
    ManifestDagMissingError,
    ManifestFileNotFoundError,
)
from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.payload import AgentSpec, RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions


def _make_connection_ref() -> ConnectionRef:
    return ConnectionRef.model_validate(
        {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/openapi/test-123",
                "name": "test-conn",
                "connectorName": "openapi",
                "adminUsers": [],
                "adminGroups": [],
                "adminRoles": [],
            },
        }
    )


class _ConcreteE2ETest(BaseE2ETest):
    """Minimal concrete subclass for unit testing BaseE2ETest without setup_method."""

    connector_short_name = "openapi"
    argo_package_name = "@atlan/openapi"
    argo_template_name = "atlan-openapi"
    mode = RunMode.DIRECT
    app_service_url = "http://openapi.svc"

    def _mustache_substitutions(self) -> MustacheSubstitutions:
        return MustacheSubstitutions(connection=_make_connection_ref())


# ---------------------------------------------------------------------------
# _apply_mustache_subs
# ---------------------------------------------------------------------------


class TestApplyMustacheSubs:
    """Recursive {{...}} replacement — exact-match only, no partial substitution."""

    def setup_method(self) -> None:
        self.harness = _ConcreteE2ETest()

    def test_replaces_matching_string(self) -> None:
        assert self.harness._apply_mustache_subs("{{foo}}", {"{{foo}}": "bar"}) == "bar"

    def test_leaves_non_matching_string(self) -> None:
        assert (
            self.harness._apply_mustache_subs("{{foo}}", {"{{other}}": "bar"})
            == "{{foo}}"
        )

    def test_partial_match_not_substituted(self) -> None:
        # Only whole-string matches are replaced; substrings are left alone.
        result = self.harness._apply_mustache_subs(
            "prefix-{{foo}}-suffix", {"{{foo}}": "bar"}
        )
        assert result == "prefix-{{foo}}-suffix"

    def test_recurses_into_dict_values(self) -> None:
        result = self.harness._apply_mustache_subs(
            {"key": "{{val}}", "nested": {"k2": "{{v2}}"}},
            {"{{val}}": "a", "{{v2}}": 42},
        )
        assert result == {"key": "a", "nested": {"k2": 42}}

    def test_dict_keys_are_not_substituted(self) -> None:
        result = self.harness._apply_mustache_subs(
            {"{{foo}}": "literal-key"}, {"{{foo}}": "bar"}
        )
        # Values are substituted but keys are preserved.
        assert result == {"{{foo}}": "literal-key"}

    def test_recurses_into_list(self) -> None:
        result = self.harness._apply_mustache_subs(
            ["{{x}}", "unchanged", {"y": "{{x}}"}],
            {"{{x}}": "replaced"},
        )
        assert result == ["replaced", "unchanged", {"y": "replaced"}]

    def test_non_string_scalar_passthrough(self) -> None:
        assert self.harness._apply_mustache_subs(42, {"{{foo}}": "bar"}) == 42

    def test_none_passthrough(self) -> None:
        assert self.harness._apply_mustache_subs(None, {"{{foo}}": "bar"}) is None

    def test_replacement_value_can_be_dict(self) -> None:
        payload = {"conn": "{{connection}}"}
        subs = {"{{connection}}": {"typeName": "Connection", "attributes": {}}}
        result = self.harness._apply_mustache_subs(payload, subs)
        assert result["conn"]["typeName"] == "Connection"


# ---------------------------------------------------------------------------
# _seed_dag_from_manifest
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, dag: dict[str, Any]) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"dag": dag}))
    return manifest


class TestSeedDagFromManifest:
    """Manifest loader: file resolution, queue patching, mustache substitution."""

    def setup_method(self) -> None:
        self.harness = _ConcreteE2ETest()
        self.harness.tenant_deployment_name = "production"  # type: ignore[attr-defined]

    # --- error paths -------------------------------------------------------

    def test_missing_file_raises(self) -> None:
        self.harness.manifest_path = "/no/such/file/manifest.json"  # type: ignore[attr-defined]
        with pytest.raises(ManifestFileNotFoundError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    def test_missing_dag_key_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"other_key": {}}))
        self.harness.manifest_path = str(manifest)  # type: ignore[attr-defined]
        with pytest.raises(ManifestDagMissingError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    def test_empty_dag_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"dag": {}}))
        self.harness.manifest_path = str(manifest)  # type: ignore[attr-defined]
        with pytest.raises(ManifestDagMissingError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    # --- happy path --------------------------------------------------------

    def test_extract_node_queue_replaced_with_caller_queue(
        self, tmp_path: Path
    ) -> None:
        dag: dict[str, Any] = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-{deployment_name}",
                "inputs": {
                    "task_queue": "atlan-openapi-{deployment_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-99")
        assert result["extract"]["inputs"]["task_queue"] == "atlan-openapi-agent-99"

    def test_non_extract_queue_substitutes_deployment_name(
        self, tmp_path: Path
    ) -> None:
        dag = {
            "publish": {
                "node_type": "workflow",
                "app_name": "publish",
                "app_task_queue": "atlan-publish-{deployment_name}",
                "inputs": {
                    "task_queue": "atlan-publish-{deployment_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert result["publish"]["inputs"]["task_queue"] == "atlan-publish-production"

    def test_app_name_placeholder_substituted(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "{app_name}",
                "app_task_queue": "atlan-{app_name}-production",
                "inputs": {
                    "task_queue": "atlan-{app_name}-production",
                    "app_name": "{app_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert result["extract"]["app_name"] == "openapi"
        assert result["extract"]["inputs"]["app_name"] == "openapi"

    def test_mustache_subs_applied_to_args(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {
                    "task_queue": "atlan-openapi-production",
                    "args": {
                        "connection": "{{connection}}",
                        "static_value": "unchanged",
                    },
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        args = result["extract"]["inputs"]["args"]
        # {{connection}} is replaced with the typed ConnectionRef dict.
        assert isinstance(args["connection"], dict)
        assert args["connection"]["typeName"] == "Connection"
        assert (
            args["connection"]["attributes"]["qualifiedName"]
            == "default/openapi/test-123"
        )
        # Static values pass through unchanged.
        assert args["static_value"] == "unchanged"

    def test_unresolved_mustache_key_left_as_is(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {
                    "task_queue": "atlan-openapi-production",
                    "args": {"unknown_key": "{{no-such-sub}}"},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        # Keys absent from the subs model are left as literal strings.
        assert result["extract"]["inputs"]["args"]["unknown_key"] == "{{no-such-sub}}"

    def test_returns_all_dag_nodes(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {"task_queue": "atlan-openapi-production", "args": {}},
            },
            "publish": {
                "node_type": "workflow",
                "app_name": "publish",
                "app_task_queue": "atlan-publish-{deployment_name}",
                "inputs": {"task_queue": "atlan-publish-{deployment_name}", "args": {}},
                "depends_on": {"node_id": "extract"},
            },
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert set(result.keys()) == {"extract", "publish"}


# ---------------------------------------------------------------------------
# setup_method — two-store / RunMode.DIRECT warning
# ---------------------------------------------------------------------------


class _AgentModeE2ETest(_ConcreteE2ETest):
    """Same as _ConcreteE2ETest but RunMode.AGENT — two-store is meaningful here."""

    mode = RunMode.AGENT
    # Skips the $admin-role AtlanClient network lookup in setup_method, which
    # is irrelevant to this test and would otherwise also log a (harmless)
    # warning against the fake tenant URL, muddying the assertion.
    connection_admin_roles = ("test-admin-role-guid",)


class TestTwoStoreDirectModeWarning:
    """ADR-0014 two-store CI wiring only has an effect under RunMode.AGENT —
    see the comment in BaseE2ETest.setup_method(). These tests exercise the
    warning-vs-silent behavior without a real tenant (the $admin-role lookup
    network call fails against the fake URL and is caught + logged
    separately by setup_method(), so admin-role attrs are set here to keep
    each test isolated to the one warning under test).
    """

    def _bootstrap_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")

    def test_warns_when_two_store_enabled_and_mode_is_direct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        monkeypatch.setenv("TWO_STORE", "true")
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        class _DirectModeTest(_ConcreteE2ETest):
            connection_admin_roles = ("test-admin-role-guid",)
            # Isolate this test to the two-store warning (disable the stall-guard
            # DIRECT warning, covered separately below).
            ae_stall_grace_seconds = 0

        _DirectModeTest().setup_method()

        assert mock_logger.warning.called
        message = mock_logger.warning.call_args[0][0]
        assert "RunMode.DIRECT" in message

    def test_no_warning_when_mode_is_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        monkeypatch.setenv("TWO_STORE", "true")
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        _AgentModeE2ETest().setup_method()

        mock_logger.warning.assert_not_called()

    def test_no_warning_when_two_store_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        monkeypatch.delenv("TWO_STORE", raising=False)
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        class _DirectModeTest(_ConcreteE2ETest):
            connection_admin_roles = ("test-admin-role-guid",)
            # Disable the stall-guard DIRECT warning so this test isolates the
            # two-store path (covered separately below).
            ae_stall_grace_seconds = 0

        _DirectModeTest().setup_method()

        mock_logger.warning.assert_not_called()


class TestStallGuardDirectModeWarning:
    """setup_method nudges toward the =0 opt-out when the stall guard is armed
    under RunMode.DIRECT, where a KEDA-idle pod can cold-start past the grace.
    """

    def _bootstrap_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")
        monkeypatch.delenv("TWO_STORE", raising=False)

    def _warn_messages(self, mock_logger: MagicMock) -> list[str]:
        return [c.args[0] for c in mock_logger.warning.call_args_list]

    def test_warns_when_direct_and_guard_armed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        class _DirectGuarded(_ConcreteE2ETest):  # DIRECT + default grace 180
            connection_admin_roles = ("test-admin-role-guid",)

        _DirectGuarded().setup_method()

        assert any(
            "ae_stall_grace_seconds" in m for m in self._warn_messages(mock_logger)
        )

    def test_no_warning_when_guard_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        class _DirectUnguarded(_ConcreteE2ETest):
            connection_admin_roles = ("test-admin-role-guid",)
            ae_stall_grace_seconds = 0

        _DirectUnguarded().setup_method()

        mock_logger.warning.assert_not_called()

    def test_no_warning_when_mode_is_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        mock_logger = MagicMock()
        monkeypatch.setattr("application_sdk.testing.e2e.base.logger", mock_logger)

        # AGENT + default grace 180 → the stall guard is fine (dedicated worker).
        _AgentModeE2ETest().setup_method()

        assert not any(
            "ae_stall_grace_seconds" in m for m in self._warn_messages(mock_logger)
        )


# ---------------------------------------------------------------------------
# _extract_task_queue
# ---------------------------------------------------------------------------


class TestExtractTaskQueue:
    """The extract task queue is the single source of truth shared by the seed
    DAG and the stall-guard diagnostic (must match the deployed worker's queue).
    """

    def test_agent_mode_uses_agent_name(self) -> None:
        class _AgentModeTest(_ConcreteE2ETest):
            mode = RunMode.AGENT

            def agent_spec(self) -> AgentSpec:
                return AgentSpec(agent_name="openapi-e2e-full-ci-42")

        assert _AgentModeTest()._extract_task_queue() == "atlan-openapi-e2e-full-ci-42"

    def test_direct_mode_falls_back_to_connector_default(self) -> None:
        # _ConcreteE2ETest is RunMode.DIRECT → agent_spec() is None.
        assert _ConcreteE2ETest()._extract_task_queue() == "atlan-openapi-default"


class TestAgentSpecDerivation:
    """AGENT mode derives the agent identity — and therefore the extract queue —
    from the worker's ATLAN_APPLICATION_NAME + ATLAN_DEPLOYMENT_NAME env, so a
    per-leg ATLAN_DEPLOYMENT_NAME (set by the CI action) isolates each matrix
    leg's queue with no per-connector hard-coding. Mirrors
    application_sdk.main._derive_task_queue's atlan-{app}-{deployment} shape.
    """

    def test_derives_agent_name_and_queue_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42-connection-create")

        class _T(_ConcreteE2ETest):
            mode = RunMode.AGENT

        spec = _T().agent_spec()
        assert spec is not None
        assert spec.agent_name == "openapi-e2e-full-ci-42-connection-create"
        # The extract node lands on exactly the worker's atlan-{app}-{deployment}
        # queue (see _derive_task_queue), byte-for-byte.
        assert (
            _T()._extract_task_queue()
            == "atlan-openapi-e2e-full-ci-42-connection-create"
        )

    def test_distinct_deployment_yields_distinct_queues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")

        class _T(_ConcreteE2ETest):
            mode = RunMode.AGENT

        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42-connection-create")
        create_q = _T()._extract_task_queue()
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42-connection-reuse")
        reuse_q = _T()._extract_task_queue()
        assert create_q != reuse_q

    def test_subclass_override_still_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42")

        class _T(_ConcreteE2ETest):
            mode = RunMode.AGENT

            def agent_spec(self) -> AgentSpec:
                return AgentSpec(agent_name="pinned-name")

        assert _T().agent_spec().agent_name == "pinned-name"

    def test_agent_mode_without_deployment_env_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # APP set, DEPLOYMENT absent → the app-only queue shape isn't derivable.
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)

        class _T(_ConcreteE2ETest):
            mode = RunMode.AGENT

        with pytest.raises(HarnessMethodNotImplementedError):
            _T().agent_spec()

    def test_agent_mode_without_application_env_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Symmetric branch: DEPLOYMENT set, APP absent → also not derivable.
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42-connection-create")

        class _T(_ConcreteE2ETest):
            mode = RunMode.AGENT

        with pytest.raises(HarnessMethodNotImplementedError):
            _T().agent_spec()


class TestStallGuardDefault:
    """The stall guard is on by default (test-harness only), and a suite that
    runs against shared / autoscaled infra can disable it by setting 0.
    """

    def test_enabled_by_default(self) -> None:
        assert _ConcreteE2ETest.ae_stall_grace_seconds == 180

    def test_subclass_can_opt_out(self) -> None:
        class _OptedOut(_ConcreteE2ETest):
            ae_stall_grace_seconds = 0

        assert _OptedOut.ae_stall_grace_seconds == 0


class TestConnectionQnUniqueness:
    """The connection QN must be unique per test instance so parallel matrix
    legs (and overlapping same-ref runs) don't collide on one connection."""

    def _bootstrap_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")

    def test_same_second_instances_get_distinct_numeric_qns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._bootstrap_env(monkeypatch)
        # Freeze the clock so both instances share an epoch — the exact
        # same-second race two parallel matrix legs can hit. The random suffix
        # must still make them distinct.
        monkeypatch.setattr("time.time", lambda: 1783979480.0)

        class _T(_ConcreteE2ETest):
            connection_admin_roles = ("test-admin-role-guid",)  # skip net lookup

        a = _T()
        a.setup_method()
        b = _T()
        b.setup_method()

        assert a.connection_qualified_name != b.connection_qualified_name
        for qn in (a.connection_qualified_name, b.connection_qualified_name):
            assert qn.startswith("default/openapi/")
            # Pure-numeric trailing segment so Atlas never rejects the name.
            assert qn.rsplit("/", 1)[-1].isdigit()
