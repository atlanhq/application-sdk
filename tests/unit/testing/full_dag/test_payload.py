"""Unit tests for the AE submit-payload builder.

Network-free; just asserts the shape of the dict against the contract
the tenant's package-workflows service expects (captured from the
devex UI).
"""

from __future__ import annotations

import orjson
import pytest

from application_sdk.testing.full_dag.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_ae_payload,
)

_CONNECTION = ConnectionSpec(
    name="full-dag-e2e-mysql-1234",
    qualified_name="default/mysql/full-dag-e2e-1234",
    connector_name="mysql",
    source_logo="https://assets.atlan.com/assets/mysql.png",
    admin_users=("aryaman",),
    admin_roles=("30502f8b-f748-4771-9b71-2a3b3b5faae0",),
)


_DATABASE = DatabaseSpec(
    host="atlan-mysql.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com",
    port=3306,
    username="atlan",
    password="hunter2",
)


def _params(payload: dict) -> dict[str, object]:
    """Flatten the Argo task parameters into a name → value dict for assertion."""
    tasks = payload["spec"]["templates"][0]["dag"]["tasks"]
    return {p["name"]: p["value"] for p in tasks[0]["arguments"]["parameters"]}


def test_direct_mode_payload_basic_shape() -> None:
    payload = build_ae_payload(
        run_id=1234,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
    )

    assert payload["execution_mode"] == "native"
    assert payload["metadata"]["name"] == "atlan-mysql-1234"
    assert (
        payload["metadata"]["app_service_url"]
        == "http://mysql.mysql-app.svc.cluster.local"
    )
    assert (
        payload["metadata"]["annotations"]["package.argoproj.io/name"] == "@atlan/mysql"
    )

    params = _params(payload)
    assert params["extraction-method"] == "direct"
    # Credential overrides flow through in DIRECT
    assert params["credential-guid.host"] == _DATABASE.host
    assert params["credential-guid.basic.username"] == _DATABASE.username
    assert params["credential-guid.basic.password"] == _DATABASE.password
    # No agent-json in direct mode
    assert "agent-json" not in params


def test_agent_mode_payload_includes_agent_json() -> None:
    agent = AgentSpec(agent_name="ci-1234")
    payload = build_ae_payload(
        run_id=1234,
        mode=RunMode.AGENT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
        agent=agent,
    )

    params = _params(payload)
    assert params["extraction-method"] == "agent"

    agent_json = orjson.loads(params["agent-json"])
    assert agent_json["agent-name"] == "ci-1234"
    assert agent_json["agent-type"] == "new-app-framework"
    assert agent_json["host"] == _DATABASE.host
    # Username/password values in agent-json are *secret-store keys*,
    # not raw credentials — caller's secret store resolves them.
    assert agent_json["basic.username"] == "SDR_MYSQL_USERNAME"
    assert agent_json["basic.password"] == "SDR_MYSQL_PASSWORD"

    # Flat agent-json.* duplicates of the same keys should exist
    assert params["agent-json.agent-name"] == "ci-1234"
    assert params["agent-json.basic.username"] == "SDR_MYSQL_USERNAME"


def test_agent_mode_without_agent_spec_raises() -> None:
    with pytest.raises(ValueError, match="agent mode requires"):
        build_ae_payload(
            run_id=1,
            mode=RunMode.AGENT,
            connector_short_name="mysql",
            argo_package_name="@atlan/mysql",
            argo_template_name="atlan-mysql",
            app_service_url="http://mysql.mysql-app.svc.cluster.local",
            connection=_CONNECTION,
            database=_DATABASE,
            agent=None,
        )


def test_connection_attributes_round_trip_through_payload() -> None:
    """The nested ``connection`` parameter must JSON-parse back to the spec."""
    payload = build_ae_payload(
        run_id=99,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
    )
    params = _params(payload)
    parsed = orjson.loads(params["connection"])
    assert parsed["typeName"] == "Connection"
    assert parsed["attributes"]["qualifiedName"] == _CONNECTION.qualified_name
    assert parsed["attributes"]["adminUsers"] == list(_CONNECTION.admin_users)


def test_direct_credential_payload_carries_full_record() -> None:
    """DIRECT mode payload[] body has the full credential — host, port,
    username, password — so the prod-deployed pod can resolve and
    connect at runtime."""
    payload = build_ae_payload(
        run_id=42,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
    )
    cred = payload["payload"][0]
    assert cred["parameter"] == "credentialGuid"
    assert cred["body"]["host"] == _DATABASE.host
    assert cred["body"]["username"] == _DATABASE.username
    assert cred["body"]["password"] == _DATABASE.password
    assert cred["body"]["connectorConfigName"] == "atlan-connectors-mysql"


def test_agent_credential_payload_omits_db_creds() -> None:
    """AGENT mode payload[] body is intentionally lightweight — no
    host/username/password — because those resolve at the agent worker
    via the local secret-store. Sending the full body causes the
    orchestrator to skip credential creation and `{{credentialGuid}}`
    stays unsubstituted as the seed DAG's "__placeholder__" string."""
    payload = build_ae_payload(
        run_id=42,
        mode=RunMode.AGENT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
        agent=AgentSpec(agent_name="mysql-ci-42"),
    )
    cred = payload["payload"][0]
    assert cred["parameter"] == "credentialGuid"
    body = cred["body"]
    assert body["connectorConfigName"] == "atlan-connectors-mysql"
    assert body["authType"] == "basic"
    # Critical: no DB-connection details — those live in agent-json
    # / secret-store, not in the credential body.
    assert "host" not in body
    assert "port" not in body
    assert "username" not in body
    assert "password" not in body


def test_ae_workflow_slug_override() -> None:
    """When the caller passes a slug, it should land verbatim in metadata.

    Required for tenants that don't auto-create workflows on submit —
    the engineer pre-creates a workflow via UI / Heracles and the test
    points at its slug.
    """
    payload = build_ae_payload(
        run_id=1234,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
        ae_workflow_slug="mysql-existing-prod-slug",
    )
    assert payload["metadata"]["ae_workflow_slug"] == "mysql-existing-prod-slug"


def test_ae_workflow_slug_defaults_to_per_run_when_blank() -> None:
    """Empty slug → fall back to the auto-generated unique-per-run slug."""
    payload = build_ae_payload(
        run_id=99,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
        ae_workflow_slug="",
    )
    assert payload["metadata"]["ae_workflow_slug"] == "mysql-e2e-99"


def test_run_id_drives_uniqueness() -> None:
    """Two payloads with different run_ids must produce different workflow names."""
    p1 = build_ae_payload(
        run_id=1,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
    )
    p2 = build_ae_payload(
        run_id=2,
        mode=RunMode.DIRECT,
        connector_short_name="mysql",
        argo_package_name="@atlan/mysql",
        argo_template_name="atlan-mysql",
        app_service_url="http://mysql.mysql-app.svc.cluster.local",
        connection=_CONNECTION,
        database=_DATABASE,
    )
    assert p1["metadata"]["name"] != p2["metadata"]["name"]
    assert p1["metadata"]["ae_workflow_slug"] != p2["metadata"]["ae_workflow_slug"]
