"""Unit tests for the refactored build_ae_payload() with typed substitutions.

Covers the openapi (no-credential) case and the SQL (with-credential) case.
Both snapshots assert the envelope shape the tenant's package-workflows
service expects; any regression here means AE would reject the submit.
"""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_ae_payload,
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
