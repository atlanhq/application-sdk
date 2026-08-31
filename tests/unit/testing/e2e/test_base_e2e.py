"""Unit tests for BaseE2ETest — manifest loader and substitution walker.

Covers the two methods the review flagged as uncovered (TEST-001/G4):
  - ``_apply_mustache_subs``: recursive {{...}} replacement
  - ``_seed_dag_from_manifest``: manifest JSON loading + queue patching + subs
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import orjson
import pytest

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e._errors import (
    DAGProgressStalledError,
    HarnessMethodNotImplementedError,
    ManifestDagMissingError,
    ManifestFileNotFoundError,
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    ProgressWatchdogUnreachableError,
    WorkerNotHealthyError,
)
from application_sdk.testing.e2e.base import (
    BaseE2ETest,
    FullDAGOutcome,
    NodeDispatch,
    _derive_progress_stall_seconds,
)
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import AgentSpec, DatabaseSpec, RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness._poll import fake_clock


@asynccontextmanager
async def _null_atlas_client() -> AsyncIterator[object]:
    """Stand-in for the Atlas client, for a test that patches what reads it."""
    yield object()


class _FakeAE:
    """Async stand-in for the AE client ``BaseE2ETest`` holds, recording calls.

    Async because everything below the pytest boundary is (FND-224's decision
    D1): the base class talks to
    :class:`~application_sdk.testing.harness.automation_engine.AEClient`
    directly now, not through the sync ``AEWorkflowClient`` shim, so a
    ``MagicMock`` would hand back coroutine-shaped nonsense.
    """

    def __init__(self, *, poll: object = None) -> None:
        self._poll = poll
        self.submits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.polls: list[dict[str, Any]] = []
        self.listed: list[tuple[str, str]] = []
        self.closed = False

    async def submit_workflow(self, payload: dict[str, Any], **kwargs: Any) -> str:
        self.submits.append((payload, kwargs))
        return "run-1"

    async def poll_native_status(self, run_id: str, **kwargs: Any) -> Any:
        self.polls.append(kwargs)
        if isinstance(self._poll, BaseException):
            raise self._poll
        return self._poll

    async def get_published_version(self, slug: str) -> None:
        return None

    async def probe_run_is_listed(self, slug: str, run_id: str) -> None:
        self.listed.append((slug, run_id))

    async def aclose(self) -> None:
        self.closed = True


def _stub_bootstrap(monkeypatch: pytest.MonkeyPatch, slug: str = "slug") -> None:
    """Replace the seed-and-publish step, which needs a tenant, with the slug."""

    async def _bootstrap(self: object) -> str:
        return slug

    monkeypatch.setattr(_ConcreteE2ETest, "_bootstrap_workflow", _bootstrap)
    monkeypatch.setattr(
        _ConcreteE2ETest, "_build_ae_payload", lambda self, slug: {"slug": slug}
    )


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

    def test_env_overrides_the_class_deployment_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A tenant whose system apps are not registered under "production" is a
        # per-leg env var (from the cross-CSP tenant matrix), not a code change.
        monkeypatch.setenv("E2E_TENANT_DEPLOYMENT_NAME", "staging")
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
        assert result["publish"]["inputs"]["task_queue"] == "atlan-publish-staging"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_env_falls_back_to_the_class_default(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unset GitHub Actions env var arrives as "", and an empty deployment
        # name would address `atlan-publish-` and fail far from its cause.
        monkeypatch.setenv("E2E_TENANT_DEPLOYMENT_NAME", value)
        assert self.harness.resolved_tenant_deployment_name() == "production"

    def test_unset_env_falls_back_to_the_class_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("E2E_TENANT_DEPLOYMENT_NAME", raising=False)
        assert self.harness.resolved_tenant_deployment_name() == "production"

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


# ---------------------------------------------------------------------------
# Source-availability tier — E2E_SOURCE_AVAILABLE gate
# ---------------------------------------------------------------------------


def _scripted_http(*responses: object) -> httpx.MockTransport:
    """A transport that answers with each item in turn, repeating the last.

    An item is either an ``int`` status or an exception to raise, so one script
    can mix "answered 503" with "refused the connection". The probe under test
    runs on a real :class:`httpx.AsyncClient` over this, rather than against a
    patched module global — the seam
    :func:`~application_sdk.testing.harness.preconditions.check_worker_health`
    offers, reached here through ``_worker_health_transport``.
    """
    script = list(responses)

    def _handle(request: httpx.Request) -> httpx.Response:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, BaseException):
            raise item
        return httpx.Response(int(item), request=request)

    return httpx.MockTransport(_handle)


class _NoSourceTest(_ConcreteE2ETest):
    # Pre-set admin roles so the source-available path never makes the
    # $admin network lookup (irrelevant here and slow).
    connection_admin_roles = ("test-admin-role-guid",)


class TestSourceAvailabilityGate:
    """E2E_SOURCE_AVAILABLE flips the harness between full-DAG and worker-up."""

    def test_false_skips_tenant_wiring_and_needs_no_tenant_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The worker-up tier must NOT require ATLAN_BASE_URL/API_KEY — the whole
        # point is that no tenant/source is wired.
        monkeypatch.setenv("E2E_SOURCE_AVAILABLE", "false")
        monkeypatch.delenv("ATLAN_BASE_URL", raising=False)
        monkeypatch.delenv("ATLAN_API_KEY", raising=False)

        harness = _NoSourceTest()
        harness.setup_method()

        assert harness.source_available is False
        # AE client + connection identity are never built on this path. Asked
        # for one anyway, the harness says why rather than handing back a client
        # pointed at nothing.
        assert not hasattr(harness, "_ae")
        with pytest.raises(MissingHarnessEnvError):
            _ = harness.client
        assert not hasattr(harness, "connection_qualified_name")

    def test_true_builds_tenant_wiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("E2E_SOURCE_AVAILABLE", "true")
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")

        harness = _NoSourceTest()
        harness.setup_method()

        assert harness.source_available is True
        assert hasattr(harness, "_ae")
        assert harness.connection_qualified_name.startswith("default/openapi/")

    def test_the_deprecated_client_is_built_only_when_it_is_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It warns on construction, so constructing it eagerly would warn every
        connector run about a symbol most of them never touch."""
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")

        harness = _NoSourceTest()
        harness.setup_method()

        assert harness._client is None
        with pytest.warns(DeprecationWarning):
            first = harness.client
        # Same instance thereafter, sharing the run's own AE pool rather than
        # opening a second one.
        assert harness.client is first
        assert first._ae is harness._ae

    def test_the_client_attribute_is_still_assignable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It was a plain attribute for as long as it existed.

        A refactor whose premise is that the public surface does not move does
        not get to take assignment away from a suite that supplies its own
        double.
        """
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")

        harness = _NoSourceTest()
        harness.setup_method()
        stand_in = SimpleNamespace()
        harness.client = stand_in  # type: ignore[assignment]

        assert harness.client is stand_in

    def test_default_is_source_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("E2E_SOURCE_AVAILABLE", raising=False)
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")

        harness = _NoSourceTest()
        harness.setup_method()

        assert harness.source_available is True

    def test_empty_env_falls_back_to_class_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A blank (empty / whitespace) E2E_SOURCE_AVAILABLE is UNSET, not False:
        # it must not silently degrade a source-having connector to worker-up.
        monkeypatch.setenv("E2E_SOURCE_AVAILABLE", "  ")
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("GITHUB_RUN_ID", "9999999")

        harness = _NoSourceTest()
        harness.setup_method()

        assert harness.source_available is True

    def test_tenant_env_still_enforced_when_source_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard: the source-available path must keep requiring the
        # tenant env (the early-return must not weaken it for full-DAG runs).
        monkeypatch.delenv("E2E_SOURCE_AVAILABLE", raising=False)
        monkeypatch.delenv("ATLAN_BASE_URL", raising=False)
        monkeypatch.delenv("ATLAN_API_KEY", raising=False)

        with pytest.raises(MissingHarnessEnvError):
            _NoSourceTest().setup_method()


class TestWorkerUpTier:
    """Worker-up-only assertions when no source is provisioned."""

    @pytest.fixture(autouse=True)
    def _fake_clock(self):
        """Run the health-probe loop on a fake clock.

        ``assert_worker_up`` waits through ``poll_until`` ->
        ``until_deadline_async``, so every test below in which the worker never
        answers 2xx runs the budget out for real. The old settings — interval
        ``0``, timeout ``1`` — made that a one-second *spin* per test, four
        seconds across this class, and interval ``0`` left "polls once and gives
        up" indistinguishable from "polls to the deadline" (FND-962).

        The interval must stay above zero under the fake: its sleep advances the
        clock, so a zero gap would never reach the deadline.
        """
        with fake_clock():
            yield

    def _harness(self, *responses: object) -> _NoSourceTest:
        """A no-source harness whose health probe runs on a scripted transport."""
        transport = _scripted_http(*responses) if responses else None

        class _Scripted(_NoSourceTest):
            # Three attempts, one interval apart: enough that a loop which gave
            # up after the first probe would fail the attempt-count assertion
            # below. Free under the fake clock above.
            worker_health_poll_interval_seconds = 1
            worker_health_timeout_seconds = 3

            def _worker_health_transport(self) -> httpx.AsyncBaseTransport | None:
                return transport

        harness = _Scripted()
        harness.source_available = False
        return harness

    def test_test_method_runs_worker_up_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = self._harness()
        calls = {"worker_up": 0, "full_dag": 0}
        monkeypatch.setattr(
            harness, "assert_worker_up", lambda: calls.__setitem__("worker_up", 1)
        )
        monkeypatch.setattr(
            harness, "run_full_dag", lambda: calls.__setitem__("full_dag", 1)
        )

        # The no-source tier asserts worker health, then raises pytest.skip so
        # a healthy worker reports SKIPPED (not a green full-DAG pass) — the
        # full DAG must never run in this tier.
        with pytest.raises(pytest.skip.Exception, match="worker-up smoke check"):
            harness.test_full_dag_runs_end_to_end()

        assert calls == {"worker_up": 1, "full_dag": 0}

    def test_test_method_fails_red_when_worker_unhealthy(self) -> None:
        # The no-source tier is a skip only when the worker is healthy. An
        # unhealthy worker must still fail RED (AssertionError), not be masked
        # by the pytest.skip — otherwise a worker that never deploys would show
        # SKIPPED instead of failing.
        harness = self._harness(httpx.ConnectError("connection refused"))
        with pytest.raises(AssertionError, match="did not become healthy"):
            harness.test_full_dag_runs_end_to_end()

    def test_assert_worker_up_passes_on_2xx(self) -> None:
        self._harness(200).assert_worker_up()  # must not raise

    def test_assert_worker_up_raises_when_never_healthy(self) -> None:
        harness = self._harness(httpx.ConnectError("connection refused"))
        with pytest.raises(AssertionError, match="did not become healthy"):
            harness.assert_worker_up()

    def test_the_worker_failure_is_typed_and_still_an_assertion_error(self) -> None:
        """Both halves of the FND-240 change, in one raise.

        The leaf is typed — ``last_error`` is a field a report can read rather
        than a fragment of a sentence, and a refused connection and a 503 point
        at different halves of a deployment. And it is still an
        ``AssertionError``, because this method's docstring promised one since it
        existed and out-of-repo connector suites are entitled to have written
        ``except AssertionError`` against it. Typing the leaf is worth doing;
        taking that clause away from the fleet to do it is not.
        """
        harness = self._harness(503)
        with pytest.raises(WorkerNotHealthyError) as excinfo:
            harness.assert_worker_up()

        error = excinfo.value
        assert isinstance(error, AssertionError)
        assert error.code == "PRECONDITION_WORKER_NOT_HEALTHY"
        assert error.url == harness.worker_health_url
        assert error.last_error == "HTTP 503"
        # It re-probed rather than giving up on the first 503: three attempts is
        # the whole budget at this interval.
        assert error.attempts == 3

    def test_a_refused_connection_and_a_5xx_are_told_apart(self) -> None:
        """The reason ``last_error`` is a field. "Nothing is listening" and "it is
        listening and unhappy" send an operator to different places."""
        harness = self._harness(httpx.ConnectError("connection refused"))
        with pytest.raises(WorkerNotHealthyError) as excinfo:
            harness.assert_worker_up()

        assert "connection refused" in (excinfo.value.last_error or "")


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

    # The fallback branch fires under three env conditions — deployment-only,
    # application-only, and both-absent — all resolving to the same run-id-keyed
    # name. Each is asserted separately so a future split of the branch can't
    # silently regress one. run_id is normally set by setup_method() from
    # GITHUB_RUN_ID; these minimal instances deliberately bypass setup_method
    # (see _ConcreteE2ETest), so they pin run_id as a class attribute rather than
    # mutating the instance post-construction. run_id must be an int — production
    # sets it via int(GITHUB_RUN_ID) (setup_method), so 42 matches that type.
    class _AgentModeFixed(_ConcreteE2ETest):
        mode = RunMode.AGENT
        run_id = 42

    def test_agent_mode_without_deployment_env_falls_back_to_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # APP set, DEPLOYMENT absent (a local run without the CI action) → the
        # two-var shape isn't derivable, so fall back to the run-id-keyed name
        # {connector}-{connection_name_prefix}-{run_id} rather than raising. This
        # is what lets connectors drop their agent_spec override entirely (T017).
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)

        spec = self._AgentModeFixed().agent_spec()
        assert spec is not None
        assert spec.agent_name == "openapi-e2e-full-ci-42"

    def test_agent_mode_without_application_env_falls_back_to_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Symmetric branch: DEPLOYMENT set, APP absent → still not the two-var
        # shape, so the same run-id fallback applies.
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "e2e-full-ci-42-connection-create")

        spec = self._AgentModeFixed().agent_spec()
        assert spec is not None
        assert spec.agent_name == "openapi-e2e-full-ci-42"

    def test_agent_mode_without_any_env_falls_back_to_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both vars absent — a fully local run with no CI context at all. The
        # third and last trigger of the fallback branch; asserted explicitly so
        # the branch's coverage is complete, not just the two one-var cases.
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)

        spec = self._AgentModeFixed().agent_spec()
        assert spec is not None
        assert spec.agent_name == "openapi-e2e-full-ci-42"


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


# ---------------------------------------------------------------------------
# _resolved_entrypoint — app-entrypoint derivation (pure logic, no I/O)
# ---------------------------------------------------------------------------


class TestResolvedEntrypoint:
    """A wrong parse silently degrades a multi-entrypoint connector to a bare
    manifest fetch (AE 404), so every derivation branch is covered here."""

    @pytest.mark.parametrize(
        ("entrypoint", "manifest_path", "expected"),
        [
            # Explicit entrypoint always wins; manifest_path is not consulted.
            ("crawler", "app/generated/miner/manifest.json", "crawler"),
            ("miner", "", "miner"),
            # Derived from a namespaced manifest subdir: <ep>/manifest.json.
            ("", "app/generated/miner/manifest.json", "miner"),
            ("", "some/root/app/generated/lineage/manifest.json", "lineage"),
            # Bare manifest (single-entrypoint) → no selector sent.
            ("", "app/generated/manifest.json", ""),
            # Unset manifest_path → no selector.
            ("", "", ""),
            # /generated/ marker present but the file isn't manifest.json.
            ("", "app/generated/miner/other.json", ""),
            # No /generated/ marker → can't derive → no selector.
            ("", "some/other/path/manifest.json", ""),
            # Deeper than one subdir under /generated/ → not a clean <ep>.
            ("", "app/generated/a/b/manifest.json", ""),
        ],
    )
    def test_derivation_branches(
        self, entrypoint: str, manifest_path: str, expected: str
    ) -> None:
        harness = _ConcreteE2ETest()
        harness.entrypoint = entrypoint  # type: ignore[misc]
        harness.manifest_path = manifest_path  # type: ignore[misc]

        assert harness._resolved_entrypoint() == expected


# ---------------------------------------------------------------------------
# agent_json() override → _build_ae_payload wiring
# ---------------------------------------------------------------------------


def _payload_params(payload: dict) -> dict[str, Any]:
    """Flatten the AE submit payload's task parameters to a name→value map."""
    tasks = payload["spec"]["templates"][0]["dag"]["tasks"]
    return {p["name"]: p["value"] for p in tasks[0]["arguments"]["parameters"]}


class TestAgentJsonOverrideReachesPayload:
    """The build_ae_payload(agent_json=...) mechanism is covered in isolation in
    test_harness_payload.py; this asserts the thin hook-forwarding — a subclass
    overriding agent_json() actually has that shape reach _build_ae_payload's
    submit body (not the AgentSpec-derived default)."""

    def test_custom_agent_json_shape_reaches_submit_payload(self) -> None:
        class _KeypairOverrideTest(_AgentModeE2ETest):
            def agent_json(self) -> dict[str, Any]:
                return {
                    "host": "db.example.com",
                    "port": 5432,
                    "auth-type": "keypair",
                    "agent-name": "openapi-e2e-ci-1234",
                    "agent-type": "new-app-framework",
                    "key-type": "single-key",
                    "secret-path": "openapi-credentials",
                }

        harness = _KeypairOverrideTest()
        # Attrs setup_method() normally derives — set directly so the test stays
        # hermetic (no env / no $admin-role network lookup).
        harness.run_id = 1234  # type: ignore[misc]
        harness.connection_display_name = "test-conn"  # type: ignore[misc]
        harness.connection_qualified_name = "default/openapi/1234"  # type: ignore[misc]

        params = _payload_params(harness._build_ae_payload("openapi-slug"))

        # The agent-json blob is the override verbatim, not the basic shape.
        blob = orjson.loads(params["agent-json"])
        assert blob["auth-type"] == "keypair"
        assert blob["host"] == "db.example.com"
        assert blob["secret-path"] == "openapi-credentials"
        # And the flat routing rows the cluster template reads follow it.
        assert params["agent-json.auth-type"] == "keypair"
        assert params["agent-json.host"] == "db.example.com"


# ---------------------------------------------------------------------------
# _submit_retry_kwargs — tenant-app cold-start budget (FND-402)
# ---------------------------------------------------------------------------


class TestSubmitRetryKwargs:
    """The cold-start budget handed to submit_workflow's existing retry loop."""

    def test_budget_is_expressed_as_retries_times_interval(self) -> None:
        """300s / 5s → 60 retries at 5s, so the loop spans the pod cold start."""
        harness = _ConcreteE2ETest()
        assert harness._submit_retry_kwargs() == {
            "retries": 60,
            "retry_sleep_seconds": 5,
        }

    def test_overridden_budget_is_honoured(self) -> None:
        """A connector may shrink the budget per-repo."""
        harness = _ConcreteE2ETest()
        harness.app_ready_timeout_seconds = 90
        harness.app_ready_poll_interval_seconds = 10
        assert harness._submit_retry_kwargs() == {
            "retries": 9,
            "retry_sleep_seconds": 10,
        }

    def test_zero_budget_defers_to_submit_workflow_defaults(self) -> None:
        """0 passes no overrides at all, rather than retries=0."""
        harness = _ConcreteE2ETest()
        harness.app_ready_timeout_seconds = 0
        assert harness._submit_retry_kwargs() == {}

    def test_zero_poll_interval_raises_a_typed_error(self) -> None:
        """A positive timeout with a 0 poll interval must not ZeroDivisionError."""
        harness = _ConcreteE2ETest()
        harness.app_ready_timeout_seconds = 300
        harness.app_ready_poll_interval_seconds = 0
        with pytest.raises(
            MissingHarnessClassAttrError, match="app_ready_poll_interval_seconds"
        ):
            harness._submit_retry_kwargs()

    def test_negative_poll_interval_raises_a_typed_error(self) -> None:
        """A negative poll interval is rejected the same way as 0."""
        harness = _ConcreteE2ETest()
        harness.app_ready_timeout_seconds = 300
        harness.app_ready_poll_interval_seconds = -5
        with pytest.raises(
            MissingHarnessClassAttrError, match="app_ready_poll_interval_seconds"
        ):
            harness._submit_retry_kwargs()

    def test_run_full_dag_passes_the_budget_to_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The budget actually reaches the submit — not just computed and dropped."""
        harness = _ConcreteE2ETest()
        harness.connection_qualified_name = "default/test/1234567890"
        # Stop right after the submit — the polls beyond it are covered elsewhere.
        harness._ae = _FakeAE(poll=RuntimeError("stop after submit"))
        _stub_bootstrap(monkeypatch)
        with pytest.raises(RuntimeError, match="stop after submit"):
            harness.run_full_dag()
        _, kwargs = harness._ae.submits[0]
        assert kwargs == {
            "slug": "slug",
            "retries": 60,
            "retry_sleep_seconds": 5,
        }


# ---------------------------------------------------------------------------
# DAG-progress watchdog sizing (FND-708)
# ---------------------------------------------------------------------------


class TestDeriveProgressStallSeconds:
    """The derived window must always be reachable by the poll loop.

    An absolute 1800s default silently disabled the watchdog on every suite
    whose ``ae_poll_timeout_seconds`` was 1800 or lower: the poll loop exits
    before the window can close, so the run burned the full ceiling.
    """

    @pytest.mark.parametrize(
        ("ceiling", "expected"),
        [
            (600, 300),  # harness default: a third floors up to the minimum
            (1800, 600),  # the suite shape that regressed — now fires at ~600s
            (5400, 1800),  # capped: a 90-min ceiling doesn't wait 30 min
            (60, 30),  # short smoke suite: the floor would be unreachable
        ],
    )
    def test_windows(self, ceiling: int, expected: int) -> None:
        assert _derive_progress_stall_seconds(ceiling) == expected

    @pytest.mark.parametrize("ceiling", [0, -1])
    def test_non_positive_ceiling_disables_the_watchdog(self, ceiling: int) -> None:
        assert _derive_progress_stall_seconds(ceiling) == 0

    @pytest.mark.parametrize("ceiling", [1, 10, 60, 300, 600, 1800, 3600, 10800])
    def test_window_is_always_strictly_below_the_ceiling(self, ceiling: int) -> None:
        """The whole point: no ceiling can put the watchdog out of reach.

        A window equal to the ceiling is the unreachable case; 0 (only possible
        on a ceiling so tight no positive window fits) is a deliberate off.
        """
        assert _derive_progress_stall_seconds(ceiling) < ceiling


class TestResolvedProgressStallSeconds:
    """Unset derives from the ceiling; a pinned value (0 included) wins."""

    def test_unset_derives_from_the_ceiling(self) -> None:
        harness = _ConcreteE2ETest()
        harness.ae_poll_timeout_seconds = 1800
        assert harness._resolved_progress_stall_seconds() == 600

    def test_pinned_value_is_honoured(self) -> None:
        class _Pinned(_ConcreteE2ETest):
            ae_poll_timeout_seconds = 1800
            dag_progress_stall_seconds = 420

        assert _Pinned()._resolved_progress_stall_seconds() == 420

    def test_pinned_zero_disables_the_watchdog(self) -> None:
        class _Disabled(_ConcreteE2ETest):
            dag_progress_stall_seconds = 0

        assert _Disabled()._resolved_progress_stall_seconds() == 0

    def test_run_full_dag_passes_the_resolved_window_to_the_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolved window actually reaches the poll — not just computed."""
        harness = _ConcreteE2ETest()
        harness.ae_poll_timeout_seconds = 1800
        harness.connection_qualified_name = "default/test/1234567890"
        harness._ae = _FakeAE(poll=RuntimeError("stop at poll"))
        _stub_bootstrap(monkeypatch)
        with pytest.raises(RuntimeError, match="stop at poll"):
            harness.run_full_dag()
        kwargs = harness._ae.polls[0]
        assert kwargs["progress_stall_seconds"] == 600


class TestWatchdogConfigValidation:
    """A pinned window at or above the ceiling is a disabled guard, so it must
    fail at setup rather than be discoverable only by reading both numbers."""

    @staticmethod
    def _degrade_to_worker_up(monkeypatch: pytest.MonkeyPatch) -> None:
        """Return from setup_method before it needs a tenant + credentials.

        The watchdog check runs ahead of this early return by design — it is a
        static configuration error, so it should surface on every tier.
        """
        monkeypatch.setenv("E2E_SOURCE_AVAILABLE", "false")

    @pytest.mark.parametrize("pinned", [1800, 3600])
    def test_pinned_at_or_above_the_ceiling_raises(
        self, monkeypatch: pytest.MonkeyPatch, pinned: int
    ) -> None:
        self._degrade_to_worker_up(monkeypatch)

        class _Unreachable(_ConcreteE2ETest):
            ae_poll_timeout_seconds = 1800
            dag_progress_stall_seconds = pinned

        with pytest.raises(
            ProgressWatchdogUnreachableError, match="can never fire"
        ) as exc:
            _Unreachable().setup_method()
        # The remedy names the window it would derive instead.
        assert "600s" in str(exc.value)

    def test_pinned_below_the_ceiling_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._degrade_to_worker_up(monkeypatch)

        class _Fine(_ConcreteE2ETest):
            ae_poll_timeout_seconds = 1800
            dag_progress_stall_seconds = 900

        _Fine().setup_method()  # must not raise

    def test_pinned_zero_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 is a deliberate opt-out, not an unreachable window."""
        self._degrade_to_worker_up(monkeypatch)

        class _Disabled(_ConcreteE2ETest):
            ae_poll_timeout_seconds = 1800
            dag_progress_stall_seconds = 0

        _Disabled().setup_method()  # must not raise

    def test_unset_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._degrade_to_worker_up(monkeypatch)
        _ConcreteE2ETest().setup_method()  # must not raise


# ---------------------------------------------------------------------------
# Stuck-node diagnostics (FND-708)
# ---------------------------------------------------------------------------


def _node(
    name: str,
    status: DAGNodeStatus,
    *,
    error: str | None = None,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
) -> DAGNodeResult:
    return DAGNodeResult(
        name=name,
        status=status,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        error_message=error,
    )


def _timed_out_result(
    nodes: list[DAGNodeResult],
    *,
    ceiling: float = 1800.0,
    stalled: float | None = 1311.0,
) -> DAGRunResult:
    return DAGRunResult(
        run_id="run-1",
        workflow_slug="slug",
        status=DAGRunStatus.RUNNING,
        nodes=nodes,
        timed_out_after_seconds=ceiling,
        seconds_since_last_progress=stalled,
    )


def _stalled_result(
    nodes: list[DAGNodeResult],
    *,
    window: float = 600.0,
    stalled: float | None = 612.0,
) -> DAGRunResult:
    """The observation attached to a ``DAGProgressStalledError``.

    The watchdog stop, not the ceiling: ``timed_out`` stays False so a
    diagnostic cannot mislabel a 600s stall as the 1800s ceiling.
    """
    return DAGRunResult(
        run_id="run-1",
        workflow_slug="slug",
        status=DAGRunStatus.RUNNING,
        nodes=nodes,
        progress_stalled_after_seconds=window,
        seconds_since_last_progress=stalled,
    )


class TestCaptureNodeDispatch:
    """The seed DAG is the only local source for a node's task queue —
    ``native-status`` reports statuses, not routing."""

    def test_prefers_the_resolved_inputs_queue(self) -> None:
        """``inputs.task_queue`` is the one _seed_dag_from_manifest substitutes
        ``{deployment_name}`` into; the node-level ``app_task_queue`` is left
        with the placeholder, so it must not win."""
        harness = _ConcreteE2ETest()
        harness._capture_node_dispatch(
            {
                "publish": {
                    "app_name": "publish",
                    "app_task_queue": "atlan-publish-{deployment_name}",
                    "inputs": {"task_queue": "atlan-publish-production"},
                }
            }
        )
        assert harness._node_dispatch == {
            "publish": NodeDispatch(
                app_name="publish", task_queue="atlan-publish-production"
            )
        }

    def test_falls_back_to_the_node_level_queue(self) -> None:
        harness = _ConcreteE2ETest()
        harness._capture_node_dispatch(
            {
                "extract": {
                    "app_name": "openapi",
                    "app_task_queue": "atlan-openapi-default",
                    "inputs": {},
                }
            }
        )
        assert harness._node_dispatch["extract"] == NodeDispatch(
            app_name="openapi", task_queue="atlan-openapi-default"
        )

    def test_missing_routing_records_an_empty_dispatch(self) -> None:
        """A node with no routing at all still gets an entry, so the renderer
        reports "not resolvable" rather than KeyError-ing on a failure path."""
        harness = _ConcreteE2ETest()
        harness._capture_node_dispatch({"weird": {"inputs": None}})
        assert harness._node_dispatch["weird"] == NodeDispatch(
            app_name="", task_queue=""
        )

    def test_the_seed_build_captures_the_dag_routing(self, tmp_path: Path) -> None:
        """The capture is wired into the real seed build, not only callable."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "dag": {
                        "publish": {
                            "app_name": "publish",
                            "inputs": {"task_queue": "atlan-publish-{deployment_name}"},
                        }
                    }
                }
            )
        )
        harness = _ConcreteE2ETest()
        harness.manifest_path = str(manifest)
        harness.run_id = 1
        # The real seed build, which is what the bootstrap publishes and what
        # carries the capture — no AE writes needed to exercise it.
        harness._build_seed_dag()
        assert (
            harness._node_dispatch["publish"].task_queue == "atlan-publish-production"
        )


class TestDagOutcomeHeadline:
    """A timed-out poll says the harness stopped watching — it is not a verdict
    on the nodes, and the old ``AE status=Running`` line never said so."""

    def test_timeout_names_the_ceiling_and_the_frozen_window(self) -> None:
        harness = _ConcreteE2ETest()
        headline = harness._dag_outcome_headline(
            _timed_out_result([_node("publish", DAGNodeStatus.PENDING)])
        )
        assert "DAG did not complete within 1800s" in headline
        assert "AE status=Running" in headline
        assert "no DAG node changed state for the last 1311s" in headline

    def test_timeout_without_a_stall_window_omits_the_clause(self) -> None:
        harness = _ConcreteE2ETest()
        headline = harness._dag_outcome_headline(
            _timed_out_result([_node("publish", DAGNodeStatus.PENDING)], stalled=None)
        )
        assert "DAG did not complete within 1800s" in headline
        assert "changed state" not in headline

    def test_terminal_run_keeps_the_plain_status_line(self) -> None:
        harness = _ConcreteE2ETest()
        result = DAGRunResult(
            run_id="run-1",
            workflow_slug="slug",
            status=DAGRunStatus.FAILED,
            nodes=[_node("publish", DAGNodeStatus.FAILED, error="boom")],
        )
        assert harness._dag_outcome_headline(result) == "AE status=Failed"

    def test_watchdog_stop_names_the_window_not_the_ceiling(self) -> None:
        """The watchdog closes first on any suite whose ceiling is <= 1800s, so
        this is the headline the FND-708 shape actually produces."""
        harness = _ConcreteE2ETest()
        headline = harness._dag_outcome_headline(
            _stalled_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "600s watchdog window closed before the poll ceiling" in headline
        assert "AE status=Running" in headline
        assert "no DAG node changed state for the last 612s" in headline
        # Reporting a ceiling here would be a lie: the poll never reached it.
        assert "did not complete within" not in headline


class TestDescribeDagNodes:
    """The three states that used to render identically as
    ``status=<X> error=None`` must now read differently."""

    def _harness(self) -> _ConcreteE2ETest:
        harness = _ConcreteE2ETest()
        harness._node_dispatch = {
            "lineage-publish": NodeDispatch(
                app_name="publish", task_queue="atlan-publish-production"
            ),
            "publish": NodeDispatch(
                app_name="publish", task_queue="atlan-publish-production"
            ),
        }
        return harness

    def test_pending_node_names_its_queue_and_the_child_workflow(self) -> None:
        """The observed shape: a node AE held at Pending for the whole poll. The
        old line read as a node failure and named no queue, which pointed the
        reader at the connector instead."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "AE reports Pending at the 1800s poll ceiling" in line
        assert "atlan-publish-production" in line
        assert "app_name=publish" in line
        assert "1311s" in line
        assert "error=None" not in line

    def test_pending_node_asserts_no_cause(self) -> None:
        """AE's Pending does not separate "nothing picked it up" from "the child
        workflow is running": on the run that motivated FND-708 the child had
        started 331ms in and was retrying through heartbeat timeouts. The line
        must not claim the queue is unpolled."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "NEVER SCHEDULED" not in line
        assert "Nothing appears to be polling" not in line

    def test_pending_node_points_at_the_child_workflow_id(self) -> None:
        """The child workflow is the only place that separates the two cases, and
        its ID is ``{ae_run_id}-{node_id}`` — so the line must carry it."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "'run-1-lineage-publish'" in line

    def test_pending_node_outside_a_timeout_omits_the_ceiling(self) -> None:
        """A terminal run can still carry a Pending node (an older service
        downgrading Skipped), where there is no ceiling to name."""
        harness = self._harness()
        result = DAGRunResult(
            run_id="run-1",
            workflow_slug="slug",
            status=DAGRunStatus.FAILED,
            nodes=[_node("lineage-publish", DAGNodeStatus.PENDING)],
        )
        line = harness._describe_dag_nodes(result)
        assert "AE reports Pending —" in line
        assert "poll ceiling" not in line

    def test_running_at_the_ceiling_names_the_queue_too(self) -> None:
        """The other observed shape: a worker took the node and stopped. Naming
        the queue points at the app that owns the stuck worker."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("publish", DAGNodeStatus.RUNNING)])
        )
        assert "STILL RUNNING at the 1800s poll ceiling" in line
        assert "atlan-publish-production" in line

    def test_pending_node_at_the_watchdog_stop_names_its_queue(self) -> None:
        """Same diagnosis, reached via the watchdog instead of the ceiling — this
        is the path the FND-708 shape now takes."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _stalled_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "AE reports Pending when the 600s progress watchdog closed" in line
        assert "atlan-publish-production" in line
        assert "app_name=publish" in line
        assert "'run-1-lineage-publish'" in line
        assert "poll ceiling" not in line

    def test_running_at_the_watchdog_stop_names_the_queue_too(self) -> None:
        """A node wedged Running is the shape the watchdog was built for, so it
        must not fall through to ``status=Running error=None``."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _stalled_result([_node("publish", DAGNodeStatus.RUNNING)])
        )
        assert "STILL RUNNING when the 600s progress watchdog closed" in line
        assert "atlan-publish-production" in line
        assert "status=Running error=None" not in line

    def test_running_without_a_timeout_is_not_dressed_up(self) -> None:
        """Only a ceiling makes "still running" meaningful; a terminal run's
        Running node (AE raced us) keeps the plain status line."""
        harness = self._harness()
        result = DAGRunResult(
            run_id="run-1",
            workflow_slug="slug",
            status=DAGRunStatus.FAILED,
            nodes=[_node("publish", DAGNodeStatus.RUNNING)],
        )
        assert harness._describe_dag_nodes(result) == (
            "  - publish: status=Running error=None"
        )

    def test_failed_node_still_leads_with_its_error(self) -> None:
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("publish", DAGNodeStatus.FAILED, error="boom")])
        )
        assert line == "  - publish: status=Failed error=boom"

    def test_succeeded_nodes_carry_their_timing(self) -> None:
        """Where the DAG got to before it stopped is what points at the stuck
        node's upstream — so successes are printed, with durations."""
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result(
                [
                    _node(
                        "extract",
                        DAGNodeStatus.SUCCEEDED,
                        started_at_ms=1_000_000,
                        completed_at_ms=1_152_000,
                    )
                ]
            )
        )
        assert line == "  - extract: succeeded in 152s"

    def test_skipped_node_is_not_reported_as_a_failure(self) -> None:
        harness = self._harness()
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("qi", DAGNodeStatus.SKIPPED)])
        )
        assert line == "  - qi: Skipped (AE did not run it)"

    def test_unknown_queue_degrades_instead_of_inventing_one(self) -> None:
        harness = _ConcreteE2ETest()
        harness._node_dispatch = {}
        line = harness._describe_dag_nodes(
            _timed_out_result([_node("lineage-publish", DAGNodeStatus.PENDING)])
        )
        assert "task queue not resolvable from the seed DAG" in line

    def test_no_nodes_is_stated_rather_than_blank(self) -> None:
        harness = _ConcreteE2ETest()
        assert harness._describe_dag_nodes(_timed_out_result([])) == (
            "  (no DAG nodes reported)"
        )


class TestFullDagAssertionMessage:
    """End of the chain: the message the operator actually reads in CI."""

    def test_timeout_message_reports_the_stall_not_a_node_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _ConcreteE2ETest()
        harness.source_available = True
        harness._node_dispatch = {
            "lineage-publish": NodeDispatch(
                app_name="publish", task_queue="atlan-publish-production"
            )
        }
        outcome = FullDAGOutcome(
            ae_result=_timed_out_result(
                [
                    _node(
                        "publish",
                        DAGNodeStatus.SUCCEEDED,
                        started_at_ms=0,
                        completed_at_ms=152_000,
                    ),
                    _node("lineage-publish", DAGNodeStatus.PENDING),
                ]
            ),
            connection_qualified_name="default/openapi/1",
            connection_in_atlas=False,
        )
        monkeypatch.setattr(_ConcreteE2ETest, "run_full_dag", lambda self: outcome)
        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()
        message = str(exc.value)
        assert "DAG did not complete within 1800s" in message
        assert "AE reports Pending at the 1800s poll ceiling" in message
        assert "'run-1-lineage-publish'" in message
        assert "atlan-publish-production" in message
        assert "publish: succeeded in 152s" in message
        # The old header called every non-successful node a failure.
        assert "Failed nodes:" not in message


class TestProgressStallDiagnostics:
    """The watchdog raises rather than returning, so the diagnostic has to be
    wired into the exception path too — otherwise the shape this work exists to
    explain is the one shape that skips the explanation."""

    def _harness(
        self, monkeypatch: pytest.MonkeyPatch, poll: BaseException
    ) -> _ConcreteE2ETest:
        harness = _ConcreteE2ETest()
        harness.ae_poll_timeout_seconds = 1800
        harness.connection_qualified_name = "default/openapi/1"
        harness._node_dispatch = {
            "lineage-publish": NodeDispatch(
                app_name="publish", task_queue="atlan-publish-production"
            )
        }
        harness._ae = _FakeAE(poll=poll)
        _stub_bootstrap(monkeypatch)
        return harness

    def test_stall_reraises_with_the_full_per_node_breakdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FND-708 shape via the watchdog: upstream succeeded, a later node
        frozen. The operator must get the queue and the child workflow here, not
        just ``name=status``."""
        harness = self._harness(
            monkeypatch,
            DAGProgressStalledError(
                message="No DAG node changed state for 600s for run run-1",
                result=_stalled_result(
                    [
                        _node(
                            "extract",
                            DAGNodeStatus.SUCCEEDED,
                            started_at_ms=0,
                            completed_at_ms=98_000,
                        ),
                        _node("lineage-publish", DAGNodeStatus.PENDING),
                    ]
                ),
            ),
        )
        with pytest.raises(DAGProgressStalledError) as exc:
            harness.run_full_dag()
        message = str(exc.value)
        assert "600s watchdog window closed before the poll ceiling" in message
        assert "AE reports Pending when the 600s progress watchdog closed" in message
        assert "atlan-publish-production" in message
        assert "app_name=publish" in message
        assert "'run-1-lineage-publish'" in message
        # Where the DAG got to is what points at the stuck node's upstream.
        assert "extract: succeeded in 98s" in message
        # The typed result stays attached for programmatic callers.
        assert exc.value.result is not None
        assert exc.value.result.progress_stalled is True

    def test_stall_without_an_attached_result_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to render from, so re-raising would only lose the original."""
        original = DAGProgressStalledError(message="stalled, no result attached")
        harness = self._harness(monkeypatch, original)
        with pytest.raises(DAGProgressStalledError) as exc:
            harness.run_full_dag()
        assert exc.value is original


# ---------------------------------------------------------------------------
# teardown_method — batched purge (FND-779)
# ---------------------------------------------------------------------------


class TestTeardownDelegatesToTheHarnessPurge:
    """``teardown_method`` decides *what* to purge; the harness decides *how*.

    The purge mechanics that used to live on this class — batching under httpx's
    URL ceiling, reading the whole listing before deleting anything, the two
    independently guarded phases — are
    :mod:`application_sdk.testing.harness.teardown` since child H, and
    ``tests/unit/testing/harness/test_teardown.py`` is where they are pinned.
    What is left to check here is the wiring: the run's *own* qualified name
    reaches the purge, the AE pool is released, and nothing on this path can
    raise over a verdict that has already been decided.
    """

    _QN = "default/openapi/1787587123106596"

    def _harness(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
        purged: list[str] = []

        async def _record(client: object, connection_qualified_name: str) -> object:
            purged.append(connection_qualified_name)
            return SimpleNamespace(purged=1, orphaned=(), errors=())

        harness = _ConcreteE2ETest()
        harness.connection_qualified_name = self._QN
        monkeypatch.setattr(
            "application_sdk.testing.e2e.base.purge_connection", _record
        )
        monkeypatch.setattr(
            _ConcreteE2ETest, "_atlas_client", lambda self: _null_atlas_client()
        )
        return harness, purged

    def test_this_runs_own_connection_is_what_gets_purged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, purged = self._harness(monkeypatch)
        harness.teardown_method(method=None)
        assert purged == [self._QN]

    def test_a_run_with_no_connection_purges_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker-up tier never mints a connection; teardown must be a no-op."""
        harness, purged = self._harness(monkeypatch)
        del harness.connection_qualified_name
        harness.teardown_method(method=None)
        assert purged == []

    def test_an_unreachable_tenant_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teardown runs after the assertions have decided the verdict.

        Raising here would replace a real failure with a cleanup error, which is
        exactly the miscue the two-phase guard exists to avoid.
        """
        harness = _ConcreteE2ETest()
        harness.connection_qualified_name = self._QN

        def _unreachable(self: object) -> Any:
            raise RuntimeError("no tenant configured")

        monkeypatch.setattr(_ConcreteE2ETest, "_atlas_client", _unreachable)
        harness.teardown_method(method=None)  # must not raise

    def test_the_ae_pool_is_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The AE client is opened per test in setup; teardown is what closes it.

        An unclosed pool survives to process exit, which a test process tolerates
        — but a suite is many tests, and the pool is bound to the loop that
        opened it.
        """
        harness, _ = self._harness(monkeypatch)
        closed: list[bool] = []

        async def _aclose() -> None:
            closed.append(True)

        harness._ae = SimpleNamespace(aclose=_aclose)
        harness.teardown_method(method=None)
        assert closed == [True]

    def test_a_pool_that_will_not_close_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, purged = self._harness(monkeypatch)

        async def _aclose() -> None:
            raise RuntimeError("transport already gone")

        harness._ae = SimpleNamespace(aclose=_aclose)
        harness.teardown_method(method=None)  # must not raise
        # ...and the purge still ran: closing is the second, independent phase.
        assert purged == [self._QN]


# ---------------------------------------------------------------------------
# resolved_connector_config_name — FND-857
# ---------------------------------------------------------------------------


class _NoDatabaseSpecTest(_ConcreteE2ETest):
    """A suite with no ``database_spec()`` hook at all (the common non-SQL shape)."""


class _LegacyFieldOnlyTest(_ConcreteE2ETest):
    """Declares the credential-config name only on the deprecated spec field.

    This is the metabase shape: a plain ``BaseE2ETest`` subclass that defines
    ``database_spec()`` ad hoc (there is no such hook on the base) and sets
    ``connector_config_name`` on the returned :class:`DatabaseSpec`.
    """

    def database_spec(self) -> DatabaseSpec:
        return DatabaseSpec(
            host="metabase",
            port=3000,
            username="u",
            password="p",
            connector_config_name="atlan-connectors-legacy",
        )


class _BothAgreeTest(_LegacyFieldOnlyTest):
    """The mysql shape: ClassVar pinned, spec field restating it."""

    connector_config_name = "atlan-connectors-legacy"


class _BothDisagreeTest(_LegacyFieldOnlyTest):
    """ClassVar and spec field give different answers — the field loses."""

    connector_config_name = "atlan-connectors-classvar"


class _BrokenSpecWithClassVarTest(_ConcreteE2ETest):
    """ClassVar answers; ``database_spec()`` blows up on an unrelated cause."""

    connector_config_name = "atlan-connectors-classvar"

    def database_spec(self) -> DatabaseSpec:
        raise RuntimeError("credentials env not set")


class TestResolvedConnectorConfigName:
    def test_classvar_is_used_when_set(self) -> None:
        class _T(_ConcreteE2ETest):
            connector_config_name = "atlan-connectors-classvar"

        assert _T().resolved_connector_config_name() == "atlan-connectors-classvar"

    def test_empty_when_nothing_declared(self) -> None:
        """Both empty leaves build_ae_payload to derive the conventional name."""
        assert _NoDatabaseSpecTest().resolved_connector_config_name() == ""

    def test_sql_default_hook_is_not_an_error(self) -> None:
        """An unoverridden ``SQLAppE2ETest.database_spec()`` raises; that is a
        "no legacy value here", not a harness failure."""

        class _T(_ConcreteE2ETest):
            def database_spec(self) -> DatabaseSpec:
                raise HarnessMethodNotImplementedError(
                    message="subclass must override database_spec()",
                    operation="database_spec",
                )

        assert _T().resolved_connector_config_name() == ""

    def test_legacy_field_is_honoured_when_classvar_empty(self) -> None:
        """The point of the ticket: the field consumers populate is not inert."""
        harness = _LegacyFieldOnlyTest()
        with pytest.warns(DeprecationWarning, match="DatabaseSpec"):
            assert harness.resolved_connector_config_name() == "atlan-connectors-legacy"

    def test_honoured_warning_says_the_value_is_being_submitted(self) -> None:
        with pytest.warns(DeprecationWarning) as record:
            _LegacyFieldOnlyTest().resolved_connector_config_name()
        message = str(record[0].message)
        assert "atlan-connectors-legacy" in message
        assert "submitting" in message
        assert "removed in v4.0" in message
        assert "connector_config_name` ClassVar" in message

    def test_classvar_wins_over_a_disagreeing_legacy_field(self) -> None:
        """Never let the hand-written copy override a generated identity."""
        harness = _BothDisagreeTest()
        with pytest.warns(DeprecationWarning, match="IGNORED"):
            assert (
                harness.resolved_connector_config_name() == "atlan-connectors-classvar"
            )

    def test_ignored_warning_names_both_values(self) -> None:
        with pytest.warns(DeprecationWarning) as record:
            _BothDisagreeTest().resolved_connector_config_name()
        message = str(record[0].message)
        assert "atlan-connectors-legacy" in message
        assert "atlan-connectors-classvar" in message

    def test_agreeing_legacy_field_still_warns_as_redundant(self) -> None:
        """Agreement today is luck, not wiring — the line still has to go."""
        harness = _BothAgreeTest()
        with pytest.warns(DeprecationWarning, match="no effect"):
            assert harness.resolved_connector_config_name() == "atlan-connectors-legacy"

    def test_broken_database_spec_does_not_break_a_declared_classvar(self) -> None:
        """The lookup is advisory once the ClassVar has answered, so a hook that
        cannot be built must not take the payload down with it."""
        harness = _BrokenSpecWithClassVarTest()
        assert harness.resolved_connector_config_name() == "atlan-connectors-classvar"

    def test_broken_database_spec_propagates_when_it_is_load_bearing(self) -> None:
        """With no ClassVar the hook is the only source, so swallowing here
        would silently fall back to the conventional name — the original trap."""

        class _T(_ConcreteE2ETest):
            def database_spec(self) -> DatabaseSpec:
                raise RuntimeError("credentials env not set")

        with pytest.raises(RuntimeError, match="credentials env not set"):
            _T().resolved_connector_config_name()

    def test_build_ae_payload_submits_the_legacy_value(self) -> None:
        """End to end: the field reaches the wire, not just the resolver.

        ``connectorConfigName`` is backfilled onto the credential body from the
        resolved credential type, so a body that does not set one is what shows
        whether the resolution reached the submit.
        """

        class _T(_LegacyFieldOnlyTest):
            def _credential_body(self) -> CredentialBody:
                return CredentialBody()

        harness = _T()
        harness.run_id = 1
        harness.connection_qualified_name = "default/openapi/1"
        harness.connection_display_name = "openapi-1"
        harness._admin_role_guid = "role-guid-123"

        with pytest.warns(DeprecationWarning):
            payload = harness._build_ae_payload("openapi-slug")

        body = payload["payload"][0]["body"]
        assert body["connectorConfigName"] == "atlan-connectors-legacy"
