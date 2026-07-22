"""Unit tests for the refactored build_ae_payload() with typed substitutions.

Covers the openapi (no-credential) case and the SQL (with-credential) case.
Both snapshots assert the envelope shape the tenant's package-workflows
service expects; any regression here means AE would reject the submit.
"""

from __future__ import annotations

from typing import Any

import orjson

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_ae_payload,
    build_agent_json,
)
from application_sdk.testing.e2e.substitutions import (
    MustacheSubstitutions,
    SQLMustacheSubstitutions,
)

_OPENAPI_CONNECTION = ConnectionSpec(
    name="e2e-ci-openapi-1234",
    qualified_name="default/openapi/e2e-ci-1234",
    connector_name="openapi",
    source_logo="https://assets.atlan.com/assets/openapi.png",
    admin_users=(),
    admin_roles=(),
)

_SQL_CONNECTION = ConnectionSpec(
    name="e2e-ci-mysql-1234",
    qualified_name="default/mysql/e2e-ci-1234",
    connector_name="mysql",
    source_logo="https://assets.atlan.com/assets/mysql.png",
    admin_users=("aryaman",),
    admin_roles=("30502f8b-f748-4771-9b71-2a3b3b5faae0",),
)


def _params(payload: dict) -> dict[str, Any]:
    tasks = payload["spec"]["templates"][0]["dag"]["tasks"]
    return {p["name"]: p["value"] for p in tasks[0]["arguments"]["parameters"]}


def _make_openapi_subs() -> MustacheSubstitutions:
    conn_ref = ConnectionRef.model_validate(
        {"typeName": "Connection", "attributes": _OPENAPI_CONNECTION.attributes()}
    )

    # Simulate the generated OpenAPIMustacheSubstitutions
    class OpenAPIMustacheSubstitutions(MustacheSubstitutions):
        from pydantic import Field

        import_type: str = Field(default="URL", alias="{{import_type}}")
        spec_url: str = Field(default="", alias="{{spec_url}}")
        spec_prefix: str = Field(default="", alias="{{spec_prefix}}")
        spec_key: str = Field(default="", alias="{{spec_key}}")
        cloud_source: str = Field(default="", alias="{{cloud_source}}")

    return OpenAPIMustacheSubstitutions(
        connection=conn_ref,
        spec_url="https://petstore3.swagger.io/api/v3/openapi.json",
    )


class TestOpenAPIPayload:
    """build_ae_payload with no credential_body (public source)."""

    def test_no_payload_key_when_no_credential(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        assert "payload" not in result

    def test_execution_mode(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        assert result["execution_mode"] == "native"
        assert result["metadata"]["name"] == "atlan-openapi-1234"

    def test_no_entrypoint_key_by_default(self) -> None:
        # Single-entrypoint apps send no selector — AE fetches the bare manifest.
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=_make_openapi_subs(),
            credential_body=None,
        )
        assert "entrypoint" not in result["metadata"]

    def test_entrypoint_injected_into_metadata_when_set(self) -> None:
        # Multi-entrypoint apps (crawler/miner, extract/lineage) must send the
        # selector or AE 404s with "No manifest available".
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="redshift",
            argo_package_name="@atlan/redshift",
            argo_template_name="atlan-redshift",
            app_service_url="http://redshift.redshift-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=_make_openapi_subs(),
            credential_body=None,
            entrypoint="crawler",
        )
        assert result["metadata"]["entrypoint"] == "crawler"
        # spec.entrypoint (Argo template) stays "main" — distinct concept.
        assert result["spec"]["entrypoint"] == "main"

    def test_connector_specific_params_emitted(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        params = _params(result)
        assert params["spec_url"] == "https://petstore3.swagger.io/api/v3/openapi.json"
        assert params["import_type"] == "URL"

    def test_connection_param_present(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        params = _params(result)
        conn_json = orjson.loads(params["connection"])
        assert conn_json["typeName"] == "Connection"
        assert conn_json["attributes"]["qualifiedName"] == "default/openapi/e2e-ci-1234"

    def test_credential_guid_default_carried_through(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        params = _params(result)
        assert params["credential-guid"] == "{{credentialGuid}}"

    def test_flat_connection_params(self) -> None:
        subs = _make_openapi_subs()
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="openapi",
            argo_package_name="@atlan/openapi",
            argo_template_name="atlan-openapi",
            app_service_url="http://openapi.openapi-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
        )
        params = _params(result)
        assert params["connection.qualifiedName"] == "default/openapi/e2e-ci-1234"
        assert params["connection.connectorName"] == "openapi"


class TestSQLPayloadWithCredential:
    """build_ae_payload with a typed CredentialBody."""

    def _make_sql_subs(self) -> SQLMustacheSubstitutions:
        conn_ref = ConnectionRef.model_validate(
            {"typeName": "Connection", "attributes": _SQL_CONNECTION.attributes()}
        )
        return SQLMustacheSubstitutions(
            connection=conn_ref,
            extraction_method="agent",
            agent_json={
                "host": "db.example.com",
                "port": 3306,
                "auth-type": "basic",
                "agent-name": "mysql-e2e-ci-1234",
                "agent-type": "new-app-framework",
                "key-type": "single-key",
                "aws-auth-method": "iam",
                "azure-auth-method": "managed_identity",
                "basic.username": "SDR_MYSQL_USERNAME",
                "basic.password": "SDR_MYSQL_PASSWORD",
            },
            include_filter='{"^def$":[".*"]}',
            exclude_filter="{}",
        )

    def _make_credential(self) -> CredentialBody:
        class MysqlCredentialBody(CredentialBody):
            from pydantic import Field

            name: str = Field(default="mysql-e2e-test")
            auth_type: str = Field(default="basic", alias="authType")
            connector_config_name: str = Field(
                default="atlan-connectors-mysql", alias="connectorConfigName"
            )
            host: str = Field(default="")
            port: int = Field(default=3306)
            username: str = Field(default="")
            password: str = Field(default="")

        return MysqlCredentialBody(
            host="db.example.com",
            port=3306,
            username="atlan",
            password="hunter2",
        )

    def test_payload_key_present_with_credential(self) -> None:
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="mysql",
            argo_package_name="@atlan/mysql",
            argo_template_name="atlan-mysql",
            app_service_url="http://mysql.mysql-app.svc.cluster.local",
            connection=_SQL_CONNECTION,
            mustache_subs=self._make_sql_subs(),
            credential_body=self._make_credential(),
        )
        assert "payload" in result
        assert result["payload"][0]["parameter"] == "credentialGuid"
        assert result["payload"][0]["type"] == "credential"

    def test_credential_body_dumped_correctly(self) -> None:
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="mysql",
            argo_package_name="@atlan/mysql",
            argo_template_name="atlan-mysql",
            app_service_url="http://mysql.mysql-app.svc.cluster.local",
            connection=_SQL_CONNECTION,
            mustache_subs=self._make_sql_subs(),
            credential_body=self._make_credential(),
        )
        body = result["payload"][0]["body"]
        assert body["authType"] == "basic"
        assert body["connectorConfigName"] == "atlan-connectors-mysql"
        assert body["host"] == "db.example.com"
        assert body["port"] == 3306

    def test_extraction_method_param(self) -> None:
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="mysql",
            argo_package_name="@atlan/mysql",
            argo_template_name="atlan-mysql",
            app_service_url="http://mysql.mysql-app.svc.cluster.local",
            connection=_SQL_CONNECTION,
            mustache_subs=self._make_sql_subs(),
            credential_body=self._make_credential(),
        )
        params = _params(result)
        assert params["extraction-method"] == "agent"

    def test_agent_json_param_is_json_string(self) -> None:
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="mysql",
            argo_package_name="@atlan/mysql",
            argo_template_name="atlan-mysql",
            app_service_url="http://mysql.mysql-app.svc.cluster.local",
            connection=_SQL_CONNECTION,
            mustache_subs=self._make_sql_subs(),
            credential_body=self._make_credential(),
        )
        params = _params(result)
        agent_json = orjson.loads(params["agent-json"])
        assert agent_json["host"] == "db.example.com"
        assert agent_json["agent-name"] == "mysql-e2e-ci-1234"


class TestBuildAgentJson:
    """build_agent_json wires DatabaseSpec.extra as dotted extra.* keys.

    Regression guard for the postgres full-DAG proof: a SQL client whose
    DB_CONFIG.required lists ``database`` (postgres, oracle, mssql, …) can't
    build a connection string without it, while mysql omits it. The connector
    supplies it once via DatabaseSpec.extra and the harness threads it through
    as ``extra.database``, which transform_agent_credentials nests into
    credentials["extra"]["database"].
    """

    _AGENT = AgentSpec(agent_name="postgres-e2e-ci-1234")

    def test_extra_keys_emitted_as_dotted_extra(self) -> None:
        db = DatabaseSpec(
            host="postgres",
            port=5432,
            username="e2e_user",
            password="e2e_pass",
            extra={"database": "e2e_main"},
        )
        aj = build_agent_json(db, self._AGENT, "postgres")
        assert aj["extra.database"] == "e2e_main"
        # standard fields still present + unchanged
        assert aj["host"] == "postgres"
        assert aj["basic.username"] == "SDR_POSTGRES_USERNAME"

    def test_empty_extra_emits_no_extra_keys(self) -> None:
        # mysql parity: no database in DB_CONFIG.required → no extra.* keys.
        db = DatabaseSpec(host="mysql", port=3306, username="u", password="p")
        aj = build_agent_json(db, self._AGENT, "mysql")
        assert not any(k.startswith("extra.") for k in aj)


_BUNDLE_AGENT_JSON = {
    "host": "my.instance.example.com",
    "port": 443,
    "auth-type": "basic",
    "agent-name": "salesforce-e2e-full-ci-1234",
    "agent-type": "new-app-framework",
    "key-type": "",
    "aws-auth-method": "iam",
    "azure-auth-method": "managed_identity",
    "secret-manager": "local",
    "secret-path": "salesforce-credentials",
    "username": "username",
    "password": "password",
    "extra": {"client_id": "client_id", "client_secret": "client_secret"},
}


class TestAgentJsonInjection:
    """build_ae_payload(agent_json=...) emits the flat agent-json.* +
    credential-guid.* routing rows (item 2) so agent-mode connectors no longer
    hand-roll a _build_ae_payload override to append them."""

    def _build(self, **kwargs):
        return build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="salesforce",
            argo_package_name="@atlan/salesforce",
            argo_template_name="atlan-salesforce",
            app_service_url="http://salesforce.salesforce-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=_make_openapi_subs(),
            credential_body=None,
            **kwargs,
        )

    def test_no_agent_json_rows_when_not_supplied(self) -> None:
        params = _params(self._build())
        assert not any(k.startswith("agent-json.") for k in params)
        assert not any(k.startswith("credential-guid.") for k in params)

    def test_blob_written_from_agent_json(self) -> None:
        params = _params(self._build(agent_json=_BUNDLE_AGENT_JSON))
        blob = orjson.loads(params["agent-json"])
        assert blob["secret-path"] == "salesforce-credentials"
        assert blob["extra"]["client_id"] == "client_id"

    def test_flat_routing_rows_emitted(self) -> None:
        params = _params(self._build(agent_json=_BUNDLE_AGENT_JSON))
        assert params["agent-json.host"] == "my.instance.example.com"
        assert params["agent-json.port"] == 443
        assert params["agent-json.auth-type"] == "basic"
        assert params["agent-json.agent-name"] == "salesforce-e2e-full-ci-1234"
        assert params["agent-json.key-type"] == ""
        assert params["agent-json.secret-manager"] == "local"
        assert params["agent-json.secret-path"] == "salesforce-credentials"

    def test_ref_key_and_nested_extra_not_flattened(self) -> None:
        # Only the routing subset is flattened; ref-keys + nested extra ride in
        # the blob (matches the salesforce inline override).
        params = _params(self._build(agent_json=_BUNDLE_AGENT_JSON))
        assert "agent-json.username" not in params
        assert "agent-json.password" not in params
        assert "agent-json.extra" not in params

    def test_credential_guid_rows(self) -> None:
        params = _params(self._build(agent_json=_BUNDLE_AGENT_JSON))
        # credential-type defaults to atlan-connectors-<short> when unset.
        assert (
            params["credential-guid.credential-type"] == "atlan-connectors-salesforce"
        )
        assert params["credential-guid.auth-type"] == "basic"

    def test_credential_type_override(self) -> None:
        params = _params(
            self._build(
                agent_json=_BUNDLE_AGENT_JSON,
                credential_type="atlan-connectors-custom",
            )
        )
        assert params["credential-guid.credential-type"] == "atlan-connectors-custom"

    def test_single_agent_json_param_no_duplicate(self) -> None:
        # mustache_subs may already carry a {{agent-json}}; the passed dict wins
        # and there must be exactly one agent-json param.
        subs = SQLMustacheSubstitutions(
            connection=ConnectionRef.model_validate(
                {"typeName": "Connection", "attributes": _SQL_CONNECTION.attributes()}
            ),
            extraction_method="agent",
            agent_json={"host": "stale", "auth-type": "basic"},
        )
        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="salesforce",
            argo_package_name="@atlan/salesforce",
            argo_template_name="atlan-salesforce",
            app_service_url="http://salesforce.salesforce-app.svc.cluster.local",
            connection=_SQL_CONNECTION,
            mustache_subs=subs,
            credential_body=None,
            agent_json=_BUNDLE_AGENT_JSON,
        )
        names = [
            p["name"]
            for p in result["spec"]["templates"][0]["dag"]["tasks"][0]["arguments"][
                "parameters"
            ]
        ]
        assert names.count("agent-json") == 1
        blob = orjson.loads(_params(result)["agent-json"])
        assert blob["host"] == "my.instance.example.com"  # passed dict wins


class TestConnectorConfigNameSetdefault:
    """build_ae_payload backfills the credential body's connectorConfigName
    (item 5) — missing it => a heracles nil-panic 500."""

    def test_backfilled_when_absent(self) -> None:
        class _MinimalBody(CredentialBody):
            from pydantic import Field

            name: str = Field(default="default-anaplan-1-1")

        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="anaplan",
            argo_package_name="@atlan/anaplan",
            argo_template_name="atlan-anaplan",
            app_service_url="http://anaplan.anaplan-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=_make_openapi_subs(),
            credential_body=_MinimalBody(),
        )
        assert (
            result["payload"][0]["body"]["connectorConfigName"]
            == "atlan-connectors-anaplan"
        )

    def test_not_overwritten_when_present(self) -> None:
        class _BodyWithName(CredentialBody):
            from pydantic import Field

            name: str = Field(default="n")
            connector_config_name: str = Field(
                default="atlan-connectors-explicit", alias="connectorConfigName"
            )

        result = build_ae_payload(
            run_id=1234,
            mode=RunMode.AGENT,
            connector_short_name="glue",
            argo_package_name="@atlan/glue",
            argo_template_name="atlan-glue",
            app_service_url="http://glue.glue-app.svc.cluster.local",
            connection=_OPENAPI_CONNECTION,
            mustache_subs=_make_openapi_subs(),
            credential_body=_BodyWithName(),
        )
        assert (
            result["payload"][0]["body"]["connectorConfigName"]
            == "atlan-connectors-explicit"
        )
