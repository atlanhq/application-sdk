"""Unit tests for SystemAppE2ETest and the tenant-pool gate (FND-3542)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pydantic
import pytest
from pydantic import Field

from application_sdk.contracts.types import ConnectionRef
from application_sdk.execution._temporal.store_assert import MAX_STORE_EXPECTATIONS
from application_sdk.testing.e2e import (
    BaseE2ETest,
    RunMode,
    StoreAbsent,
    StoreExpectation,
    SystemAppE2ETest,
    TenantPool,
)
from application_sdk.testing.e2e._errors import (
    AtlanApiHttpError,
    MissingHarnessClassAttrError,
    StoreAssertDisabledError,
    StoreAssertQueueAmbiguousError,
    StoreAssertUnreadableError,
    TenantPoolMismatchError,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.e2e.system_app import STORE_ASSERT_NODE_ID
from application_sdk.testing.e2e.tenant_pool import TENANT_POOL_ENV, check_tenant_pool
from application_sdk.testing.harness.automation_engine.wire import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)


def _connection_ref() -> ConnectionRef:
    return ConnectionRef.model_validate(
        {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/postgres/run-1",
                "name": "e2e",
                "connectorName": "postgres",
                "adminUsers": [],
                "adminGroups": [],
                "adminRoles": [],
            },
        }
    )


class _DeleteSubstitutions(MustacheSubstitutions):
    """The shape a system app's own substitutions take: one field per placeholder."""

    connection_qualified_name: str = Field(alias="{{connection-qualified-name}}")
    delete_type: str = Field(alias="{{delete-type}}")


class _ConnectorSuite(BaseE2ETest):
    connector_short_name = "openapi"
    argo_package_name = "@atlan/openapi"
    argo_template_name = "atlan-openapi"
    mode = RunMode.DIRECT
    # Pre-set so setup never makes the $admin network lookup.
    connection_admin_roles = ("admin-role-guid",)


class _SystemSuite(SystemAppE2ETest):
    # No argo_* on purpose: a system suite sends no Heracles envelope.
    connector_short_name = "connection-delete"
    connection_admin_roles = ("admin-role-guid",)
    required_dag_nodes = ("delete",)

    def _mustache_substitutions(self) -> MustacheSubstitutions:
        return _DeleteSubstitutions.model_validate(
            {
                "{{connection}}": _connection_ref(),
                "{{connection-qualified-name}}": "default/postgres/target",
                "{{delete-type}}": "PURGE",
            }
        )


@pytest.fixture
def tenant_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
    monkeypatch.setenv("ATLAN_API_KEY", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "9999999")
    monkeypatch.delenv(TENANT_POOL_ENV, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# check_tenant_pool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("required", "env", "expected"),
    [
        pytest.param(
            TenantPool.CONNECTOR, {}, TenantPool.CONNECTOR, id="unset-is-connector"
        ),
        pytest.param(
            TenantPool.CONNECTOR,
            {TENANT_POOL_ENV: " "},
            TenantPool.CONNECTOR,
            id="blank-is-connector",
        ),
        pytest.param(
            TenantPool.CONNECTOR,
            {TENANT_POOL_ENV: "connector"},
            TenantPool.CONNECTOR,
            id="connector-on-connector",
        ),
        pytest.param(
            TenantPool.SYSTEM,
            {TENANT_POOL_ENV: "system"},
            TenantPool.SYSTEM,
            id="system-on-system",
        ),
    ],
)
def test_matching_pool_is_allowed(
    required: TenantPool, env: dict[str, str], expected: TenantPool
) -> None:
    assert check_tenant_pool(required, env, suite="S") is expected


@pytest.mark.parametrize(
    ("required", "env"),
    [
        pytest.param(TenantPool.SYSTEM, {}, id="system-suite-unset"),
        pytest.param(
            TenantPool.SYSTEM,
            {TENANT_POOL_ENV: "connector"},
            id="system-suite-connector",
        ),
        pytest.param(
            TenantPool.CONNECTOR,
            {TENANT_POOL_ENV: "system"},
            id="connector-suite-system",
        ),
        pytest.param(TenantPool.SYSTEM, {TENANT_POOL_ENV: "sytem"}, id="unknown-value"),
        pytest.param(
            TenantPool.CONNECTOR, {TENANT_POOL_ENV: "SYSTEM"}, id="case-is-not-folded"
        ),
    ],
)
def test_mismatched_or_unknown_pool_is_refused(
    required: TenantPool, env: dict[str, str]
) -> None:
    with pytest.raises(TenantPoolMismatchError) as exc:
        check_tenant_pool(required, env, suite="S")
    assert TENANT_POOL_ENV in str(exc.value)


# ---------------------------------------------------------------------------
# setup_method applies the gate in both directions
# ---------------------------------------------------------------------------


def test_connector_suite_is_refused_on_the_system_pool(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    tenant_env.setenv(TENANT_POOL_ENV, "system")
    with pytest.raises(TenantPoolMismatchError, match="_ConnectorSuite"):
        _ConnectorSuite().setup_method()


def test_connector_suite_runs_with_no_pool_set(tenant_env: pytest.MonkeyPatch) -> None:
    harness = _ConnectorSuite()
    harness.setup_method()
    assert harness.connection_qualified_name


@pytest.mark.parametrize("pool", [None, "connector"])
def test_system_suite_is_refused_off_the_system_pool(
    tenant_env: pytest.MonkeyPatch, pool: str | None
) -> None:
    if pool is not None:
        tenant_env.setenv(TENANT_POOL_ENV, pool)
    with pytest.raises(TenantPoolMismatchError, match="_SystemSuite"):
        _SystemSuite().setup_method()


def test_system_suite_runs_on_the_system_pool_without_argo_names(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    tenant_env.setenv(TENANT_POOL_ENV, "system")
    harness = _SystemSuite()
    harness.setup_method()
    assert harness.connection_qualified_name


def test_system_suite_still_requires_its_name(tenant_env: pytest.MonkeyPatch) -> None:
    tenant_env.setenv(TENANT_POOL_ENV, "system")

    class _Nameless(SystemAppE2ETest):
        pass

    with pytest.raises(MissingHarnessClassAttrError):
        _Nameless().setup_method()


def test_a_subclass_of_a_system_suite_stays_on_the_system_pool(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    class _Child(_SystemSuite):
        pass

    with pytest.raises(TenantPoolMismatchError):
        _Child().setup_method()


# ---------------------------------------------------------------------------
# Submit path
# ---------------------------------------------------------------------------


class _FakeAE:
    def __init__(self) -> None:
        self.direct: list[str] = []
        self.heracles: list[dict[str, Any]] = []

    async def submit_published_version(self, slug: str, **kwargs: Any) -> str:
        self.direct.append(slug)
        return "run-direct"

    async def submit_workflow(self, payload: dict[str, Any], **kwargs: Any) -> str:
        self.heracles.append(payload)
        return "run-heracles"


async def test_system_suite_submits_straight_to_ae() -> None:
    harness = _SystemSuite()
    fake = _FakeAE()
    harness._ae = fake  # type: ignore[assignment]

    run_id = await harness._submit({"envelope": "unused"}, slug="slug-1")

    assert run_id == "run-direct"
    assert fake.direct == ["slug-1"]
    assert fake.heracles == []


async def test_deployed_manifest_check_never_runs_on_the_system_path() -> None:
    class _Asserting(_SystemSuite):
        # Even a suite that turns the check on gets none: there is no republish
        # to compare against, and waiting for one would only time out.
        assert_deployed_manifest = True

    harness = _Asserting()
    harness._expected_node_identities = {"delete": object()}  # type: ignore[assignment]

    async def _must_not_read(slug: str) -> None:
        raise AssertionError("read the published version on the system path")

    harness._read_superseding_published_version = _must_not_read  # type: ignore[method-assign]
    await harness._assert_deployed_manifest_matches("slug-1")


# ---------------------------------------------------------------------------
# Run-scoped args reach the seed DAG through the substitutions hook
# ---------------------------------------------------------------------------


def test_seed_dag_takes_run_scoped_args_from_the_substitutions_hook(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dag": {
                    "delete": {
                        "inputs": {
                            "task_queue": "atlan-connection-delete-{deployment_name}",
                            "args": {
                                "connection_qualified_name": "{{connection-qualified-name}}",
                                "delete_type": "{{delete-type}}",
                            },
                        }
                    }
                }
            }
        )
    )

    class _WithManifest(_SystemSuite):
        manifest_path = str(manifest)

    dag = _WithManifest()._seed_dag_from_manifest("atlan-ci-queue")

    inputs = dag["delete"]["inputs"]
    assert inputs["args"] == {
        "connection_qualified_name": "default/postgres/target",
        "delete_type": "PURGE",
    }
    # Not named "extract", so it keeps the tenant's queue — where the installed
    # system app polls — rather than the CI worker's.
    assert inputs["task_queue"] == "atlan-connection-delete-production"


# ---------------------------------------------------------------------------
# Object-store expectations (FND-3571)
# ---------------------------------------------------------------------------

_PURGED = "persistent-artifacts/default/postgres/target"
_QUEUE = "atlan-connection-delete-{deployment_name}"


def _manifest(tmp_path: Path, dag: dict[str, Any] | None = None) -> str:
    manifest = tmp_path / "manifest.json"
    default = {
        "delete": {
            "app_name": "connection-delete",
            "inputs": {"task_queue": _QUEUE, "args": {}},
        }
    }
    manifest.write_text(json.dumps({"dag": dag or default}))
    return str(manifest)


def _absent() -> StoreExpectation:
    return StoreAbsent(prefix=_PURGED)


def _suite(manifest: str, *expectations: StoreExpectation) -> _SystemSuite:
    class _Asserting(_SystemSuite):
        manifest_path = manifest

        def store_expectations(self) -> list[StoreExpectation]:
            return list(expectations)

    return _Asserting()


_ALL_OK = (
    ("delete", DAGNodeStatus.SUCCEEDED),
    (STORE_ASSERT_NODE_ID, DAGNodeStatus.SUCCEEDED),
)


def _ae_result(nodes: tuple[tuple[str, DAGNodeStatus], ...] = _ALL_OK) -> DAGRunResult:
    return DAGRunResult(
        run_id="run-1",
        workflow_slug="slug-1",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[DAGNodeResult(n, s, None, None, None) for n, s in nodes],
    )


class _OutputsAE:
    def __init__(self, outputs: dict[str, Any] | Exception) -> None:
        self.outputs = outputs
        self.reads: list[tuple[str, str]] = []

    async def get_node_outputs(self, run_id: str, node_id: str) -> dict[str, Any]:
        self.reads.append((run_id, node_id))
        if isinstance(self.outputs, Exception):
            raise self.outputs
        return self.outputs


def test_no_expectations_appends_no_node(tmp_path: Path) -> None:
    assert set(_suite(_manifest(tmp_path))._build_seed_dag()) == {"delete"}


def test_expectations_append_a_node_on_the_apps_queue(tmp_path: Path) -> None:
    harness = _suite(_manifest(tmp_path), _absent())

    dag = harness._build_seed_dag()

    node = dag[STORE_ASSERT_NODE_ID]
    assert node["inputs"]["workflow_type"] == "sdk:store-assert"
    assert node["inputs"]["task_queue"] == "atlan-connection-delete-production"
    assert node["app_name"] == "connection-delete"
    assert node["depends_on"] == {"node_id": "delete", "tag": "success"}
    assert node["inputs"]["args"] == {
        "expectations": [{"prefix": _PURGED, "kind": "absent"}]
    }
    assert harness._node_dispatch[STORE_ASSERT_NODE_ID].task_queue == (
        "atlan-connection-delete-production"
    )


def test_node_waits_on_every_other_node(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "a": {"inputs": {"task_queue": _QUEUE}},
            "b": {"inputs": {"task_queue": _QUEUE}},
        },
    )
    dag = _suite(manifest, _absent())._build_seed_dag()
    assert dag[STORE_ASSERT_NODE_ID]["depends_on"] == {
        "and_conditions": [
            {"node_id": "a", "tag": "success"},
            {"node_id": "b", "tag": "success"},
        ]
    }


def test_several_queues_need_an_override(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"a": {"inputs": {"task_queue": "q1"}}, "b": {"inputs": {"task_queue": "q2"}}},
    )
    with pytest.raises(StoreAssertQueueAmbiguousError, match="store_assert_task_queue"):
        _suite(manifest, _absent())._build_seed_dag()


def test_too_many_expectations_fail_before_submit(tmp_path: Path) -> None:
    too_many = [_absent()] * (MAX_STORE_EXPECTATIONS + 1)
    with pytest.raises(pydantic.ValidationError):
        _suite(_manifest(tmp_path), *too_many)._build_seed_dag()


def _verdict(passed: bool) -> dict[str, Any]:
    return {
        "enabled": True,
        "passed": passed,
        "observations": [
            {
                "prefix": _PURGED,
                "kind": "absent",
                "passed": passed,
                "objects_all": 0 if passed else 4,
            }
        ],
    }


async def _read_then_grade(
    tmp_path: Path,
    ae: _OutputsAE,
    nodes: tuple[tuple[str, DAGNodeStatus], ...] = _ALL_OK,
) -> None:
    harness = _suite(_manifest(tmp_path), _absent())
    harness._ae = ae  # type: ignore[assignment]
    # Set by setup_method on a real run; the base outcome carries it.
    harness.connection_qualified_name = "default/postgres/run-1"
    outcome = await harness._read_atlas(_ae_result(nodes))
    harness._assert_full_dag_outcome(outcome)


async def test_a_passing_verdict_grades_green(tmp_path: Path) -> None:
    ae = _OutputsAE(_verdict(True))
    await _read_then_grade(tmp_path, ae)
    assert ae.reads == [("run-1", STORE_ASSERT_NODE_ID)]


async def test_a_failing_verdict_is_an_assertion_naming_the_prefix(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError, match=r"\[FAIL\] persistent-artifacts/") as exc:
        await _read_then_grade(tmp_path, _OutputsAE(_verdict(False)))
    assert "objects (incl. markers)=4" in str(exc.value)


@pytest.mark.parametrize(
    "outputs",
    [
        pytest.param(
            AtlanApiHttpError(message="boom", target="GET runs"), id="ae-read-fails"
        ),
        pytest.param({"passed": "not-a-bool"}, id="unparseable-verdict"),
    ],
)
async def test_an_unreadable_verdict_is_never_a_pass(
    tmp_path: Path, outputs: dict[str, Any] | Exception
) -> None:
    with pytest.raises(StoreAssertUnreadableError):
        await _read_then_grade(tmp_path, _OutputsAE(outputs))


@pytest.mark.parametrize(
    "outputs",
    [
        pytest.param(
            {"enabled": False, "passed": False, "observations": []}, id="disabled"
        ),
        # A verdict that predates the gate has no `enabled`; it must not pass.
        pytest.param({"passed": True, "observations": []}, id="no-enabled-field"),
    ],
)
async def test_a_disabled_tenant_is_a_precondition_error_not_a_pass(
    tmp_path: Path, outputs: dict[str, Any]
) -> None:
    with pytest.raises(StoreAssertDisabledError, match="ATLAN_STORE_ASSERT_ENABLED"):
        await _read_then_grade(tmp_path, _OutputsAE(outputs))


async def test_a_skipped_assert_node_is_never_a_pass(tmp_path: Path) -> None:
    """The skip-tolerant DAG gate lets a Skipped node through; the store
    grader must not."""
    ae = _OutputsAE(_verdict(True))
    with pytest.raises(StoreAssertUnreadableError, match="carries no"):
        await _read_then_grade(
            tmp_path,
            ae,
            (
                ("delete", DAGNodeStatus.SUCCEEDED),
                (STORE_ASSERT_NODE_ID, DAGNodeStatus.SKIPPED),
            ),
        )
    assert ae.reads == []
