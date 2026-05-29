"""Unit tests for SQLAppE2ETest, build_seed_dag, build_agent_json, FullDAGOutcome."""

from __future__ import annotations

from application_sdk.testing.e2e.base import FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_agent_json,
    build_seed_dag,
)
from application_sdk.testing.e2e.sql_app import SQLAppE2ETest

_CONNECTION = ConnectionSpec(
    name="e2e-ci-mysql-1",
    qualified_name="default/mysql/e2e-ci-1",
    connector_name="mysql",
    source_logo="https://assets.atlan.com/assets/mysql.png",
    admin_roles=("role-guid-123",),
)

_DATABASE = DatabaseSpec(
    host="db.example.com",
    port=3306,
    username="user",
    password="pass",
    connector_config_name="atlan-connectors-mysql",
)

_AGENT = AgentSpec(agent_name="mysql-e2e-ci-1")


def _make_dag_result(
    status: DAGRunStatus, *node_statuses: DAGNodeStatus
) -> DAGRunResult:
    nodes = [
        DAGNodeResult(
            name=f"node{i}",
            status=s,
            started_at_ms=None,
            completed_at_ms=None,
            error_message=None,
        )
        for i, s in enumerate(node_statuses)
    ]
    return DAGRunResult(
        run_id="run-1", workflow_slug="slug-1", status=status, nodes=nodes
    )


# ---------------------------------------------------------------------------
# FullDAGOutcome
# ---------------------------------------------------------------------------


class TestFullDAGOutcome:
    def test_succeeded_when_all_nodes_ok_and_connection_in_atlas(self) -> None:
        result = _make_dag_result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
        outcome = FullDAGOutcome(
            ae_result=result,
            connection_qualified_name="default/mysql/1",
            connection_in_atlas=True,
        )
        assert outcome.succeeded is True

    def test_not_succeeded_when_connection_missing(self) -> None:
        result = _make_dag_result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
        outcome = FullDAGOutcome(
            ae_result=result,
            connection_qualified_name="default/mysql/1",
            connection_in_atlas=False,
        )
        assert outcome.succeeded is False

    def test_not_succeeded_when_node_failed(self) -> None:
        result = _make_dag_result(
            DAGRunStatus.FAILED, DAGNodeStatus.SUCCEEDED, DAGNodeStatus.FAILED
        )
        outcome = FullDAGOutcome(
            ae_result=result,
            connection_qualified_name="default/mysql/1",
            connection_in_atlas=True,
        )
        assert outcome.succeeded is False

    def test_default_asset_counts_empty(self) -> None:
        result = _make_dag_result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
        outcome = FullDAGOutcome(
            ae_result=result,
            connection_qualified_name="default/mysql/1",
            connection_in_atlas=True,
        )
        assert outcome.asset_counts == {}

    def test_lineage_present_false_by_default(self) -> None:
        result = _make_dag_result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
        outcome = FullDAGOutcome(
            ae_result=result,
            connection_qualified_name="default/mysql/1",
            connection_in_atlas=True,
        )
        assert outcome.lineage_present is False


# ---------------------------------------------------------------------------
# build_agent_json
# ---------------------------------------------------------------------------


class TestBuildAgentJson:
    def test_basic_fields_present(self) -> None:
        result = build_agent_json(_DATABASE, _AGENT, "mysql")
        assert result["host"] == "db.example.com"
        assert result["port"] == 3306
        assert result["auth-type"] == "basic"
        assert result["agent-name"] == "mysql-e2e-ci-1"
        assert result["agent-type"] == "new-app-framework"

    def test_sdr_credential_keys_use_connector_name(self) -> None:
        result = build_agent_json(_DATABASE, _AGENT, "mysql")
        assert result["basic.username"] == "SDR_MYSQL_USERNAME"
        assert result["basic.password"] == "SDR_MYSQL_PASSWORD"

    def test_connector_name_uppercased_in_sdr_keys(self) -> None:
        result = build_agent_json(_DATABASE, _AGENT, "mssql")
        assert result["basic.username"] == "SDR_MSSQL_USERNAME"
        assert result["basic.password"] == "SDR_MSSQL_PASSWORD"

    def test_agent_spec_fields_propagated(self) -> None:
        agent = AgentSpec(
            agent_name="my-agent",
            key_type="multi-key",
            aws_auth_method="role",
            azure_auth_method="service_principal",
        )
        result = build_agent_json(_DATABASE, agent, "mysql")
        assert result["key-type"] == "multi-key"
        assert result["aws-auth-method"] == "role"
        assert result["azure-auth-method"] == "service_principal"


# ---------------------------------------------------------------------------
# build_seed_dag
# ---------------------------------------------------------------------------


class TestBuildSeedDag:
    def test_returns_five_standard_nodes(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
        )
        assert set(dag.keys()) == {
            "extract",
            "qi",
            "publish",
            "lineage-app",
            "lineage-publish",
        }

    def test_extract_task_queue_set_on_extract_node(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-99",
            connection=_CONNECTION,
        )
        assert dag["extract"]["inputs"]["task_queue"] == "atlan-mysql-agent-99"
        assert dag["extract"]["app_task_queue"] == "atlan-mysql-agent-99"

    def test_extract_workflow_type_defaults_to_connector_name(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
        )
        assert dag["extract"]["inputs"]["workflow_type"] == "mysql"

    def test_extract_workflow_type_override(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
            extract_workflow_type="mysql-metadata-extractor",
        )
        assert dag["extract"]["inputs"]["workflow_type"] == "mysql-metadata-extractor"

    def test_connection_qualified_name_in_qi_args(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
        )
        assert (
            dag["qi"]["inputs"]["args"]["connection_qualified_name"]
            == "default/mysql/e2e-ci-1"
        )

    def test_agent_mode_includes_agent_json_in_extract_args(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
            mode=RunMode.AGENT,
            agent=_AGENT,
            database=_DATABASE,
        )
        assert "agent_json" in dag["extract"]["inputs"]["args"]
        assert (
            dag["extract"]["inputs"]["args"]["agent_json"]["host"] == "db.example.com"
        )

    def test_direct_mode_omits_agent_json(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
            mode=RunMode.DIRECT,
        )
        assert "agent_json" not in dag["extract"]["inputs"]["args"]

    def test_publish_task_queue_propagated(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            publish_task_queue="atlan-publish-staging",
            connection=_CONNECTION,
        )
        assert dag["publish"]["inputs"]["task_queue"] == "atlan-publish-staging"
        assert dag["lineage-publish"]["inputs"]["task_queue"] == "atlan-publish-staging"

    def test_lineage_app_depends_on_qi_and_publish(self) -> None:
        dag = build_seed_dag(
            connector_short_name="mysql",
            extract_task_queue="atlan-mysql-agent-1",
            connection=_CONNECTION,
        )
        conditions = dag["lineage-app"]["depends_on"]["and_conditions"]
        node_ids = {c["node_id"] for c in conditions}
        assert node_ids == {"qi", "publish"}


# ---------------------------------------------------------------------------
# SQLAppE2ETest
# ---------------------------------------------------------------------------


class _ConcreteSQLTest(SQLAppE2ETest):
    connector_short_name = "mysql"
    argo_package_name = "@atlan/mysql"
    argo_template_name = "atlan-mysql"
    mode = RunMode.AGENT
    app_service_url = "http://mysql.svc"

    def database_spec(self) -> DatabaseSpec:
        return _DATABASE

    def _credential_body(self):
        return None

    @staticmethod
    def _resolve_admin_role_guid() -> str:
        return "role-guid-123"


class TestSQLAppE2ETestHooks:
    def _make_test(self, mode: RunMode = RunMode.AGENT) -> _ConcreteSQLTest:
        t = _ConcreteSQLTest()
        t.run_id = 1
        t.connection_qualified_name = "default/mysql/1"
        t.connection_display_name = "mysql-1"
        t._admin_role_guid = "role-guid-123"
        t.__class__.mode = mode
        return t

    def test_agent_spec_returns_none_in_direct_mode(self) -> None:
        t = self._make_test(RunMode.DIRECT)
        assert t.agent_spec() is None

    def test_agent_spec_returns_agent_in_agent_mode(self) -> None:
        t = self._make_test(RunMode.AGENT)
        spec = t.agent_spec()
        assert spec is not None
        assert "mysql" in spec.agent_name
        assert str(t.run_id) in spec.agent_name

    def test_connection_spec_uses_admin_role_guid(self) -> None:
        t = self._make_test()
        spec = t.connection_spec()
        assert "role-guid-123" in spec.admin_roles

    def test_mustache_substitutions_returns_sql_type(self) -> None:
        from application_sdk.testing.e2e.substitutions import SQLMustacheSubstitutions

        t = self._make_test()
        subs = t._mustache_substitutions()
        assert isinstance(subs, SQLMustacheSubstitutions)

    def test_build_legacy_seed_dag_returns_five_nodes(self) -> None:
        t = self._make_test()
        agent = t.agent_spec()
        assert agent is not None
        extract_queue = f"atlan-{agent.agent_name}"
        dag = t._build_legacy_seed_dag(extract_queue)
        assert set(dag.keys()) == {
            "extract",
            "qi",
            "publish",
            "lineage-app",
            "lineage-publish",
        }

    def test_build_legacy_seed_dag_sets_extract_queue(self) -> None:
        t = self._make_test()
        dag = t._build_legacy_seed_dag("atlan-mysql-ci-agent-1")
        assert dag["extract"]["inputs"]["task_queue"] == "atlan-mysql-ci-agent-1"
