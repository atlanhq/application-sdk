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
from application_sdk.testing.full_dag.payload import AgentSpec, DatabaseSpec

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
                    "credential": "{{credential}}",
                    "credential_guid": "{{credential-guid}}",
                    "connection": "{{connection}}",
                    "extraction_method": "{{extraction-method}}",
                    "agent_json": "{{agent-json}}",
                    "include_filter": "{{include-filter}}",
                    "exclude_filter": "{{exclude-filter}}",
                    "exclude_table_regex": "{{exclude-table-regex}}",
                    "preflight_check": "{{preflight-check}}",
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

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test-queue")

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

    assert dag["extract"]["inputs"]["task_queue"] == "atlan-mysql-e2e-full-ci-9999999"
    assert dag["qi"]["inputs"]["task_queue"] == "atlan-query-intelligence-production"
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

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    assert dag["qi"]["inputs"]["task_queue"] == "atlan-query-intelligence-staging"
    assert dag["publish"]["inputs"]["task_queue"] == "atlan-publish-staging"


def test_seed_dag_substitutes_mustache_placeholders(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mustache ``{{...}}`` placeholders are filled with runtime values.

    AE does NOT runtime-substitute these in the seed args — they have
    to be concrete by the time the seed version is published, or the
    worker receives them as literal placeholder strings and hangs.
    Only ``{{credentialGuid}}`` (camelCase, set on credential_guid)
    survives because AE *does* substitute that one from the submit's
    payload[].body.
    """
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    extract_args = dag["extract"]["inputs"]["args"]
    # credential_guid forwards to AE via the camelCase placeholder
    assert extract_args["credential_guid"] == "{{credentialGuid}}"
    # connection is the full Connection entity, not a placeholder
    assert isinstance(extract_args["connection"], dict)
    assert extract_args["connection"]["typeName"] == "Connection"
    # filter is the literal string the test class configures
    assert extract_args["include_filter"] == instance.include_filter
    # extraction_method is filled with the mode value
    assert extract_args["extraction_method"] == "agent"
    # agent_json is the actual agent dict in AGENT mode
    assert isinstance(extract_args["agent_json"], dict)
    assert extract_args["agent_json"]["agent-name"] == "mysql-test"
    # publish's connection_entity is the full Connection JSON too
    assert isinstance(dag["publish"]["inputs"]["args"]["connection_entity"], dict)
    # No literal {{...}} left anywhere
    import json as _json

    rendered = _json.dumps(dag)
    # {{credentialGuid}} is the only acceptable remaining Mustache
    assert "{{credential-guid}}" not in rendered
    assert "{{connection}}" not in rendered
    assert "{{include-filter}}" not in rendered
    assert "{{extraction-method}}" not in rendered
    assert "{{agent-json}}" not in rendered


def test_seed_dag_preserves_publish_creation_flags(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All publish flags carry through untouched — regression guard for
    the silent-update-only-mode bug we hit on run 25788289284. Boolean
    flags don't get touched by Mustache substitution; only the
    ``connection_entity`` placeholder is filled."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")

    pub_args = dag["publish"]["inputs"]["args"]
    assert pub_args["connection_creation_enabled"] is True
    assert pub_args["executor_enabled"] is True
    assert pub_args["connection_cache_enabled"] is True
    # connection_entity is filled with the full Connection JSON
    assert isinstance(pub_args["connection_entity"], dict)
    assert pub_args["connection_entity"]["typeName"] == "Connection"
    attrs = pub_args["connection_entity"]["attributes"]
    assert attrs["connectorName"] == "mysql"
    # The QN on the Connection attributes carries the run-stamped id
    assert attrs["qualifiedName"].startswith("default/mysql/e2e-full-ci-")


def test_seed_dag_direct_mode_agent_json_is_null(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIRECT mode (no agent_spec) substitutes ``{{agent-json}}`` with None.

    The worker reads credentials straight from the credential the
    credential_guid points at, so there's no agent_json bundle to ship.
    """
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    cls.mode = RunMode.DIRECT
    cls.agent_spec = lambda self: None  # type: ignore[assignment]
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-default")
    extract_args = dag["extract"]["inputs"]["args"]
    assert extract_args["agent_json"] is None
    assert extract_args["extraction_method"] == "direct"


def test_seed_dag_result_is_json_serialisable(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substituted DAG must be a pure JSON tree — it's about to be
    POSTed as the seed version body. Catches accidentally embedding
    non-serialisable objects (Enum / dataclass / function) in the subs.
    """
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")
    # If this raises TypeError the substitution leaked a non-JSON value
    rendered = json.dumps(dag)
    assert len(rendered) > 0


def test_seed_dag_substitution_only_replaces_exact_mustache_keys(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Substring matches inside a longer string are NOT replaced — the
    substitution only fires on exact ``{{X}}`` string values."""
    # Add a dummy node arg with a string that *contains* but doesn't
    # equal a Mustache placeholder. Should be left alone.
    p = manifest_file
    raw = json.loads(p.read_text())
    raw["dag"]["extract"]["inputs"]["args"]["some_label"] = (
        "label-{{connection}}-suffix"
    )
    p.write_text(json.dumps(raw))

    _bootstrap_env(monkeypatch)
    cls = _make_test(str(p))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")
    # The substring-containing label is left as-is
    assert (
        dag["extract"]["inputs"]["args"]["some_label"] == "label-{{connection}}-suffix"
    )
    # And the exact-match Mustache field WAS substituted
    assert isinstance(dag["extract"]["inputs"]["args"]["connection"], dict)


def test_seed_dag_credential_guid_forwards_to_ae(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{{credential-guid}}`` is the one Mustache we forward — replaced
    with ``{{credentialGuid}}`` (camelCase) which AE *does* substitute
    at submit time from the payload[].body credential."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(str(manifest_file))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")
    assert dag["extract"]["inputs"]["args"]["credential_guid"] == "{{credentialGuid}}"


def test_seed_dag_substitution_walks_nested_lists(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mustache values inside list args are still substituted."""
    p = manifest_file
    raw = json.loads(p.read_text())
    raw["dag"]["extract"]["inputs"]["args"]["nested_list"] = [
        "{{include-filter}}",
        "plain-string",
        ["{{exclude-filter}}", 42],
    ]
    p.write_text(json.dumps(raw))

    _bootstrap_env(monkeypatch)
    cls = _make_test(str(p))
    instance = cls()
    instance.setup_method()

    dag = instance._seed_dag_from_manifest(extract_task_queue="atlan-mysql-test")
    nested = dag["extract"]["inputs"]["args"]["nested_list"]
    assert nested[0] == instance.include_filter
    assert nested[1] == "plain-string"
    assert nested[2][0] == instance.exclude_filter
    assert nested[2][1] == 42


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
