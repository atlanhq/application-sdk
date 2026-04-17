"""Validation tests for real-world agent_json payloads.

Exercises the three-step pipeline (substitute → expand_dotted → flatten_auth_section)
against actual payloads from production connectors: CloudSQL Postgres, Kafka,
BigQuery (GCP WIF), Postgres (access-key), and Oracle.
"""

from __future__ import annotations

import json

from application_sdk.credentials.agent import resolve_agent_json
from application_sdk.infrastructure.secrets import InMemorySecretStore


def _store_with(**bundles: str) -> InMemorySecretStore:
    store = InMemorySecretStore()
    for path, raw in bundles.items():
        store.set(path, raw)
    return store


def _bundle(**values: str) -> str:
    return json.dumps(values)


# ---- 1. CloudSQL Postgres — basic auth, dotted basic.* + extra.* ----

CLOUDSQL_POSTGRES = json.dumps(
    {
        "connectBy": "host",
        "agent-name": "cloudsql-postgres-agent",
        "agent-type": "new-app-framework",
        "key-type": "multi-key",
        "secret-path": "atlan/dev/cloudsql",
        "aws-auth-method": "iam",
        "azure-auth-method": "managed_identity",
        "extra.compiled_url": "postgresql+asyncpg://34.122.182.89:5432/postgres",
        "host": "34.122.182.89",
        "port": 5432,
        "auth-type": "basic",
        "basic.username": "username",
        "basic.password": "password",
        "extra.database": "postgres",
    }
)


class TestCloudSQLPostgres:
    async def test_basic_fields_substituted_and_nested(self) -> None:
        # In production the bundle keys match the ref-key values in agent_json.
        # "postgres" is the ref-key for extra.database; the bundle maps it
        # to the real database name.
        store = _store_with(
            **{
                "atlan/dev/cloudsql": _bundle(
                    username="real_pg_user",
                    password="real_pg_pass",
                    postgres="real_pg_db",
                )
            }
        )
        r = await resolve_agent_json(CLOUDSQL_POSTGRES, store)

        # Literals preserved
        assert r["host"] == "34.122.182.89"
        assert r["port"] == 5432
        assert r["auth-type"] == "basic"

        # basic.username/password expanded + substituted + flattened to root
        assert r["username"] == "real_pg_user"
        assert r["password"] == "real_pg_pass"

        # extra.* expanded into nested dict + flattened to root
        assert isinstance(r["extra"], dict)
        assert r["extra"]["database"] == "real_pg_db"
        assert (
            r["extra"]["compiled_url"]
            == "postgresql+asyncpg://34.122.182.89:5432/postgres"
        )

        # No dotted keys remain
        assert not any("." in k for k in r)


# ---- 2. Kafka — noauth, three-level dotted keys (noauth.extra.*) ----

KAFKA = json.dumps(
    {
        "host": "host.docker.internal:19092",
        "port": 9092,
        "auth-type": "noauth",
        "agent-name": "kafka-agent",
        "secret-manager": "awssecretmanager",
        "secret-path": "atlan-dev-kafka",
        "aws-region": "ap-south-1",
        "aws-auth-method": "iam",
        "azure-auth-method": "managed_identity",
        "noauth.extra.security_protocol": "PLAINTEXT",
        "noauth.extra.includeSchemaRegistry": "true",
        "noauth.extra.schemaRegistryHost": "https://host.docker.internal:28081",
    }
)


class TestKafka:
    async def test_three_level_dotted_keys_expand_and_flatten(self) -> None:
        # Kafka noauth has no real credential refs to substitute —
        # the values are all literal config, but they come as
        # noauth.extra.* three-level dotted keys.
        store = _store_with(**{"atlan-dev-kafka": _bundle()})
        r = await resolve_agent_json(KAFKA, store)

        # Literals
        assert r["host"] == "host.docker.internal:19092"
        assert r["port"] == 9092
        assert r["auth-type"] == "noauth"

        # noauth.extra.* → {"noauth": {"extra": {...}}} → flatten_auth_section
        # promotes noauth section to root → extra at root
        assert isinstance(r["extra"], dict)
        assert r["extra"]["security_protocol"] == "PLAINTEXT"
        assert r["extra"]["includeSchemaRegistry"] == "true"
        assert r["extra"]["schemaRegistryHost"] == "https://host.docker.internal:28081"

        # No dotted keys remain
        assert not any("." in k for k in r)


# ---- 3. BigQuery — gcp-wif auth, mixed root extra.* + gcp-wif.extra.* ----

BIGQUERY_GCP_WIF = json.dumps(
    {
        "auth-type": "gcp-wif",
        "host": "https://bigquery.googleapis.com",
        "port": 443,
        "agent-name": "bigquery-gke-bq-agent",
        "secret-manager": "gcpsecretmanager",
        "aws-auth-method": "iam",
        "azure-auth-method": "managed_identity",
        "extra.connect_type": "public",
        "basic.password": "",
        "basic.username": "",
        "gcp-wif.extra.project_id": "development-platform-370010",
        "gcp-wif.extra.service_account_email": "hariharan@development-platform-370010.iam.gserviceaccount.com",
        "gcp-wif.extra.wif_pool_provider_id": "//iam.googleapis.com/projects/546276822164/locations/global/workloadIdentityPools/test-wif-for-python-requests/providers/warelines-models-atlan-com",
        "gcp-wif.extra.atlan_oauth_id": "oauth-client-92a2403d-e1ac-4514-9ad5-835dac0801c1",
        "gcp-wif.extra.atlan_oauth_secret": "atlan-oauth-client-secret-warelines-models",
    }
)


class TestBigQueryGcpWif:
    async def test_root_extra_and_auth_section_extra_deep_merge(self) -> None:
        """extra.connect_type (root-level) and gcp-wif.extra.project_id
        (auth-section-level) must deep-merge into a single ``extra`` dict.
        """
        # gcp-wif values are literal config (not refs), bundle is empty
        store = _store_with(**{"dummy-path": _bundle()})
        # Need a secret-path for the pipeline; patch it in
        payload = json.loads(BIGQUERY_GCP_WIF)
        payload["secret-path"] = "dummy-path"
        r = await resolve_agent_json(json.dumps(payload), store)

        assert r["auth-type"] == "gcp-wif"
        assert r["host"] == "https://bigquery.googleapis.com"

        # extra must contain BOTH root-level and gcp-wif-level fields
        assert isinstance(r["extra"], dict)
        assert r["extra"]["connect_type"] == "public"
        assert r["extra"]["project_id"] == "development-platform-370010"
        assert "service_account_email" in r["extra"]
        assert "wif_pool_provider_id" in r["extra"]
        assert "atlan_oauth_id" in r["extra"]
        assert "atlan_oauth_secret" in r["extra"]

        # Empty basic.* should still expand (to empty strings)
        assert r.get("basic") == {"password": "", "username": ""} or "basic" in r

        # No dotted keys remain
        assert not any("." in k for k in r)


# ---- 4. Postgres — basic auth, access-key, ARN secret-path ----

POSTGRES_ACCESS_KEY = json.dumps(
    {
        "connectBy": "host",
        "agent-type": "new-app-framework",
        "agent-name": "postgres-agent",
        "secret-manager": "awssecretmanager",
        "secret-path": "arn:aws:secretsmanager:ap-south-1:026090536792:secret:dev/atlan/postgres-all-q5ukS6",
        "aws-region": "ap-south-1",
        "aws-auth-method": "access-key",
        "azure-auth-method": "managed_identity",
        "extra.compiled_url": "postgresql://qa-postgres-1.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com:5432/testing?defaultRowFetchSize=10000&ApplicationName=atlan&loginTimeout=5&connectionTimeout=5&tcpKeepAlive=true&readOnly=true&prepareThreshold=0",
        "host": "qa-postgres-1.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com",
        "port": 5432,
        "auth-type": "basic",
        "basic.username": "basic_username",
        "basic.password": "basic_password",
        "extra.database": "testing",
    }
)


class TestPostgresAccessKey:
    async def test_arn_secret_path_and_named_refs(self) -> None:
        """Ref-keys here are ``basic_username`` and ``basic_password``
        (not ``username``/``password``). The bundle maps them to real values.
        """
        store = _store_with(
            **{
                "arn:aws:secretsmanager:ap-south-1:026090536792:secret:dev/atlan/postgres-all-q5ukS6": _bundle(
                    basic_username="prod_user",
                    basic_password="prod_pass",
                    testing="prod_db",
                )
            }
        )
        r = await resolve_agent_json(POSTGRES_ACCESS_KEY, store)

        assert r["host"] == "qa-postgres-1.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com"
        assert r["port"] == 5432

        # basic.username ref "basic_username" → substituted → nested → flattened
        assert r["username"] == "prod_user"
        assert r["password"] == "prod_pass"

        assert isinstance(r["extra"], dict)
        assert r["extra"]["database"] == "prod_db"
        assert "compiled_url" in r["extra"]

        assert not any("." in k for k in r)


# ---- 5. Oracle — basic auth, extra.sid + extra.databaseName ----

ORACLE = json.dumps(
    {
        "host": "oracle-atlan.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com",
        "port": 1521,
        "auth-type": "basic",
        "agent-name": "oracle-agent",
        "secret-manager": "awssecretmanager",
        "secret-path": "atlan/oracle/instacart",
        "aws-region": "ap-south-1",
        "aws-auth-method": "access-key",
        "azure-auth-method": "managed_identity",
        "basic.username": "username",
        "basic.password": "password",
        "extra.sid": "ATLAN",
        "extra.databaseName": "ATLAN",
    }
)


class TestOracle:
    async def test_oracle_sid_and_database_name_in_extra(self) -> None:
        store = _store_with(
            **{
                "atlan/oracle/instacart": _bundle(
                    username="oracle_user",
                    password="oracle_pass",
                )
            }
        )
        r = await resolve_agent_json(ORACLE, store)

        assert r["host"] == "oracle-atlan.crmgvlgwn1cx.ap-south-1.rds.amazonaws.com"
        assert r["port"] == 1521

        # Credentials substituted and flattened to root
        assert r["username"] == "oracle_user"
        assert r["password"] == "oracle_pass"

        # Oracle-specific extra fields preserved as literals
        assert r["extra"]["sid"] == "ATLAN"
        assert r["extra"]["databaseName"] == "ATLAN"

        assert not any("." in k for k in r)
