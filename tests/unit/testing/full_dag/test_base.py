"""Unit tests for the BaseFullDAGE2ETest manifest-loading + substitution logic.

Network-free. Exercises only the seed-DAG plumbing — the HTTP-driven
flow (`run_full_dag`, AE submit + poll, Atlas probe) is covered by
``test_client.py`` against mocked transports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.full_dag import BaseFullDAGE2ETest, RunMode
from application_sdk.testing.full_dag.payload import (
    AgentSpec,
    DatabaseSpec,
)

# Minimum-viable manifest mirroring the mysql-app shape with `{app_name}`
# and `{deployment_name}` placeholders intact. Used by tests that want
# to verify substitution semantics in isolation.
_MANIFEST: dict[str, Any] = {
    "execution_mode": "automation-engine",
    "dag": {
        "extract": {
            "activity_name": "execute_workflow",
            "activity_display_name": "Extract MySQL Metadata",
            "app_name": "{app_name}",
            "inputs": {
                "workflow_type": "mysql",
                "app_name": "{app_name}",
                "task_queue": "atlan-mysql-{deployment_name}",
                "args": {
                    "credential_guid": "{{credential-guid}}",
                    "connection": "{{connection}}",
                    "include_filter": "{{include-filter}}",
                },
            },
        },
        "qi": {
            "activity_name": "execute_workflow",
            "app_name": "automation-engine",
            "inputs": {
                "workflow_type": "QueryIntelligenceWorkflow",
                "task_queue": "atlan-query-intelligence-{deployment_name}",
                "args": {
                    "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
                    "input_prefix": "$.extract.outputs.transformed_data_prefix",
                },
            },
            "depends_on": {"node_id": "extract"},
        },
        "publish": {
            "activity_name": "execute_workflow",
            "app_name": "publish",
            "inputs": {
                "workflow_type": "PublishWorkflow",
                "task_queue": "atlan-publish-{deployment_name}",
                "args": {
                    "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
                    "connection_creation_enabled": True,
                    "executor_enabled": True,
                    "connection_entity": "{{connection}}",
                    "connection_cache_enabled": True,
                },
            },
            "depends_on": {"node_id": "extract"},
        },
    },
}


def _make_test(manifest_path: str = "app/generated/manifest.json"):
    """Build a runnable subclass with minimal required fields stubbed."""

    class _Concrete(BaseFullDAGE2ETest):
        connector_short_name = "mysql"
        argo_package_name = "@atlan/mysql"
        argo_template_name = "atlan-mysql"
        mode = RunMode.AGENT
        app_service_url = "http://mysql.mysql-app.svc.cluster.local"

    _Concrete.manifest_path = manifest_path

    def database_spec(self):
        return DatabaseSpec(host="mysql", port=3306, username="u", password="p")

    def agent_spec(self):
        return AgentSpec(agent_name="mysql-test")

    _Concrete.database_spec = database_spec
    _Concrete.agent_spec = agent_spec
    return _Concrete


def _bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env the base class' setup_method requires.

    Manifest tests don't need a real tenant; the env vars only have to
    be non-empty so :meth:`setup_method` doesn't bail.
    """
    monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
    monkeypatch.setenv("ATLAN_API_KEY", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "9999999")


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    """Write the canonical manifest fixture into a tmp file and return its path."""
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(_MANIFEST))
    return p


def test_seed_dag_from_manifest_substitutes_app_name(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{app_name}`` is filled with ``connector_short_name`` everywhere it appears."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(
        extract_task_queue="atlan-mysql-test-queue"
    )

    assert dag["extract"]["app_name"] == "mysql"
    assert dag["extract"]["inputs"]["app_name"] == "mysql"
    # No leftover {app_name} anywhere
    assert "{app_name}" not in json.dumps(dag)


def test_seed_dag_substitutes_deployment_name_per_node(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract uses CI queue; everything else uses ``tenant_deployment_name``."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(
        extract_task_queue="atlan-mysql-e2e-full-ci-9999999"
    )

    assert (
        dag["extract"]["inputs"]["task_queue"] == "atlan-mysql-e2e-full-ci-9999999"
    )
    assert (
        dag["qi"]["inputs"]["task_queue"]
        == "atlan-query-intelligence-production"
    )
    assert dag["publish"]["inputs"]["task_queue"] == "atlan-publish-production"
    # Default tenant_deployment_name = production. No literal {deployment_name}
    # template strings should leak.
    assert "{deployment_name}" not in json.dumps(dag)


def test_seed_dag_tenant_deployment_name_override(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override ``tenant_deployment_name`` to land on a non-prod tenant queue."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    cls.tenant_deployment_name = "staging"
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(
        extract_task_queue="atlan-mysql-test"
    )

    assert dag["qi"]["inputs"]["task_queue"] == "atlan-query-intelligence-staging"
    assert dag["publish"]["inputs"]["task_queue"] == "atlan-publish-staging"


def test_seed_dag_preserves_mustache_placeholders(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AE-substituted ``{{...}}`` placeholders must reach AE intact."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(
        extract_task_queue="atlan-mysql-test"
    )

    extract_args = dag["extract"]["inputs"]["args"]
    assert extract_args["credential_guid"] == "{{credential-guid}}"
    assert extract_args["connection"] == "{{connection}}"
    assert extract_args["include_filter"] == "{{include-filter}}"
    assert dag["publish"]["inputs"]["args"]["connection_entity"] == "{{connection}}"


def test_seed_dag_preserves_publish_creation_flags(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All six publish flags carry through untouched — regression guard for
    the silent-update-only-mode bug we hit on run 25788289284."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    pub_args = dag["publish"]["inputs"]["args"]
    assert pub_args["connection_creation_enabled"] is True
    assert pub_args["executor_enabled"] is True
    assert pub_args["connection_cache_enabled"] is True
    # connection_entity stays as the Mustache placeholder for AE to fill
    assert pub_args["connection_entity"] == "{{connection}}"


def test_seed_dag_preserves_jsonpath_extract_outputs(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$.extract.outputs.X`` references must reach AE intact (AE resolves them)."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    assert (
        dag["qi"]["inputs"]["args"]["connection_qualified_name"]
        == "$.extract.outputs.connection_qualified_name"
    )
    assert (
        dag["publish"]["inputs"]["args"]["connection_qualified_name"]
        == "$.extract.outputs.connection_qualified_name"
    )


def test_seed_dag_preserves_depends_on(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``depends_on`` blocks pass through untouched."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    assert dag["qi"]["depends_on"] == {"node_id": "extract"}
    assert dag["publish"]["depends_on"] == {"node_id": "extract"}


def test_seed_dag_missing_manifest_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing manifest file surfaces a remediation-pointing RuntimeError."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(tmp_path / "does-not-exist.json"))
    instance = cls()
    instance.setup_method()

    with pytest.raises(RuntimeError, match="Manifest file not found"):
        instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")


def test_seed_dag_malformed_manifest_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest without a top-level ``dag`` block fails loud, not silent."""
    _bootstrap_env(monkeypatch)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"execution_mode": "automation-engine"}))
    cls = _make_test(str(p))
    instance = cls()
    instance.setup_method()

    with pytest.raises(RuntimeError, match="no top-level `dag` object"):
        instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")


def test_seed_dag_relative_path_resolved_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``manifest_path`` is resolved against the test's cwd —
    matches how the harness is invoked from a connector repo root."""
    _bootstrap_env(monkeypatch)
    # Put the manifest under a synthetic "connector repo" subdir.
    repo = tmp_path / "connector-repo"
    (repo / "app/generated").mkdir(parents=True)
    (repo / "app/generated/manifest.json").write_text(json.dumps(_MANIFEST))
    monkeypatch.chdir(repo)

    cls = _make_test()  # default 'app/generated/manifest.json'
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")
    assert "extract" in dag
    assert dag["extract"]["app_name"] == "mysql"
