"""Build the AE submit payload for full-DAG e2e tests.

Captured shape from the devex UI by inspecting the network tab on a real
"submit MySQL extract" click. The payload nests the same connection
attributes in three places (top-level ``payload[].body``,
``parameters['connection']`` as a JSON string, and a long list of flat
``connection.<key>`` parameter rows) — this duplication is what the
tenant's package-workflows service expects, so we faithfully reproduce
it from a single :class:`ConnectionSpec` source of truth.

Two modes:

* :data:`RunMode.DIRECT` — the AE workflow's ``extract`` activity
  dispatches to ``task_queue: atlan-<app>-<deployment>`` and the
  tenant's *production-deployed* connector pod picks it up. Tier 5.
* :data:`RunMode.AGENT` — same queue, but ``agent-name`` routes to a
  caller-deployed worker (e.g. CI-side compose with a unique
  agent-name + S3 binding). Tier 4.

The submitter's responsibility is to ensure the connector worker that
listens on the resulting Temporal queue is actually running before the
AE DAG dispatches to it. For tier 4 that's the docker compose worker;
for tier 5 that's the prod-deployed pod.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import orjson

from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions


class RunMode(str, Enum):
    """Whether the connector runs in tenant or in caller-controlled CI."""

    DIRECT = "direct"
    AGENT = "agent"


@dataclass(frozen=True)
class ConnectionSpec:
    """Identity of the Connection the AE workflow will create.

    The ``qualified_name`` should be unique per test run — the tenant
    treats Connection QN as the upsert key, so colliding QNs across
    test runs will overwrite each other and confuse asset assertions.
    Convention: ``default/<connector>/<test-prefix>-<run_id>``.
    """

    name: str
    qualified_name: str
    connector_name: str  # mysql, mssql, etc — must match the @atlan/<conn> package
    source_logo: str
    admin_users: tuple[str, ...] = ()
    admin_groups: tuple[str, ...] = ()
    admin_roles: tuple[str, ...] = ()
    category: str = "warehouse"
    row_limit: int = 10_000
    allow_query: bool = True
    allow_query_preview: bool = True
    is_discoverable: bool = True
    is_editable: bool = False
    default_credential_guid: str = ""

    def attributes(self) -> dict[str, Any]:
        """Pyatlan-style ``Connection.attributes`` block.

        ``defaultCredentialGuid`` is only included when
        ``default_credential_guid`` is non-empty — callers that have no
        credential (public sources) omit it so Atlas never receives the
        unsubstituted ``{{credentialGuid}}`` literal.
        """
        attrs: dict[str, Any] = {
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "allowQuery": self.allow_query,
            "allowQueryPreview": self.allow_query_preview,
            "rowLimit": self.row_limit,
            "connectorName": self.connector_name,
            "sourceLogo": self.source_logo,
            "isDiscoverable": self.is_discoverable,
            "isEditable": self.is_editable,
            "category": self.category,
            "adminUsers": list(self.admin_users),
            "adminGroups": list(self.admin_groups),
            "adminRoles": list(self.admin_roles),
        }
        if self.default_credential_guid:
            attrs["defaultCredentialGuid"] = self.default_credential_guid
        return attrs


@dataclass(frozen=True)
class DatabaseSpec:
    """DB connection details for the credential payload.

    Used by :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`
    to build its :class:`~application_sdk.testing.e2e.substitutions.SQLMustacheSubstitutions`
    and the codegen'd ``<Connector>CredentialBody`` override.
    """

    host: str
    port: int
    username: str
    password: str
    auth_type: str = "basic"
    extra: dict[str, Any] = field(default_factory=dict)
    connector_config_name: str = ""  # e.g. atlan-connectors-mysql


@dataclass(frozen=True)
class AgentSpec:
    """Optional agent routing (tier 4 only).

    ``agent_name`` is used by the Argo cluster template to derive the
    Temporal task queue (``atlan-<app>-<agent_name>``). The CI worker
    listens on the same queue; AE then dispatches the extract to it.
    """

    agent_name: str
    agent_type: str = "new-app-framework"
    key_type: str = "single-key"
    aws_auth_method: str = "iam"
    azure_auth_method: str = "managed_identity"


def build_agent_json(
    database: DatabaseSpec,
    agent: AgentSpec,
    connector_short_name: str,
) -> dict[str, Any]:
    """Build the agent_json routing block for AGENT-mode extract args."""
    return {
        "host": database.host,
        "port": database.port,
        "auth-type": database.auth_type,
        "agent-name": agent.agent_name,
        "agent-type": agent.agent_type,
        "key-type": agent.key_type,
        "aws-auth-method": agent.aws_auth_method,
        "azure-auth-method": agent.azure_auth_method,
        "basic.username": f"SDR_{connector_short_name.upper()}_USERNAME",
        "basic.password": f"SDR_{connector_short_name.upper()}_PASSWORD",
    }


def build_seed_dag(
    *,
    connector_short_name: str,
    extract_task_queue: str,
    publish_task_queue: str = "atlan-publish-production",
    qi_task_queue: str = "atlan-query-intelligence-production",
    lineage_task_queue: str = "atlan-lineage-production",
    connection: ConnectionSpec,
    include_filter: str = '{"^def$":[".*"]}',
    exclude_filter: str = "{}",
    temp_table_regex: str = "",
    extract_workflow_type: str | None = None,
    qi_parsing_mode: str = "lorien-only",
    qi_mine_output_type: str = "json",
    qi_input_prefix_field: str = "view_data_prefix",
    lake_provider: str = "aws",
    mode: RunMode = RunMode.DIRECT,
    agent: AgentSpec | None = None,
    database: DatabaseSpec | None = None,
) -> dict[str, Any]:
    """Build a seed-version DAG matching the connector's manifest.json shape.

    Workflows need at least one PUBLISHED version before package-
    workflows submit will accept them; the seed version is a no-op
    placeholder (uses ``credential_guid: __placeholder__``) that
    package-workflows replaces on every submit. What matters for the
    seed is that the DAG topology + task_queue references are correct
    — the actual extract args get overridden at submit time.

    Per-connector overrides (subclasses pass via kwargs):
        - ``extract_workflow_type`` — must match what the connector's
          worker actually registers as. v3 mysql registers ``"mysql"``
          (just the connector name); v2 mssql registers
          ``"mssql-metadata-extractor"``. Default = ``connector_short_name``
          (the v3 convention); override for v2-style connectors.
        - ``qi_parsing_mode`` — ``"lorien-only"`` (mssql) vs.
          ``"competitive"`` (mysql per devex sample); driven by what
          the connector emits as parseable SQL.
        - Task queue names default to the tenant's production publish/
          qi/lineage queues; override for non-production tenants.
    """
    if extract_workflow_type is None:
        extract_workflow_type = connector_short_name

    extract_args: dict[str, Any] = {
        "credential_guid": "{{credentialGuid}}",
        "connection": {
            "connection_name": connection.name,
            "connection_qualified_name": connection.qualified_name,
        },
        "extraction_method": mode.value,
        "include_filter": include_filter,
        "exclude_filter": exclude_filter,
        "temp_table_regex": temp_table_regex,
    }
    if mode is RunMode.AGENT and agent is not None and database is not None:
        extract_args["agent_json"] = build_agent_json(
            database, agent, connector_short_name
        )

    return {
        "extract": {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": f"Extract {connector_short_name.title()} Metadata",
            "app_name": connector_short_name,
            "app_task_queue": extract_task_queue,
            "inputs": {
                "workflow_type": extract_workflow_type,
                "task_queue": extract_task_queue,
                "args": extract_args,
            },
        },
        "qi": {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Parse View Lineage",
            "app_name": "query-intelligence",
            "app_task_queue": qi_task_queue,
            "inputs": {
                "workflow_type": "QueryIntelligenceWorkflow",
                "task_queue": qi_task_queue,
                "args": {
                    "connection_qualified_name": connection.qualified_name,
                    "vendor_name": connector_short_name,
                    "sql_key": "attributes.definition",
                    "catalog_key": "attributes.databaseName",
                    "schema_key": "attributes.schemaName",
                    "timestamp_key": "",
                    "mine_output_type": qi_mine_output_type,
                    "parsing_mode": qi_parsing_mode,
                    "lake_provider": lake_provider,
                    "storage_bucket": "$.extract.outputs.storage_bucket",
                    "input_prefix": f"$.extract.outputs.{qi_input_prefix_field}",
                    "output_prefix": "$.extract.outputs.view_lineage_output_prefix",
                },
            },
            "depends_on": {"node_id": "extract"},
        },
        "publish": {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Publish to Atlas",
            "app_name": "publish",
            "app_task_queue": publish_task_queue,
            "inputs": {
                "workflow_type": "PublishWorkflow",
                "task_queue": publish_task_queue,
                "args": {
                    "connection_qualified_name": "",
                    "transformed_data_prefix": "$.extract.outputs.transformed_data_prefix",
                    "publish_state_prefix": "$.extract.outputs.publish_state_prefix",
                    "current_state_prefix": "$.extract.outputs.current_state_prefix",
                },
            },
            "depends_on": {"node_id": "extract"},
        },
        "lineage-app": {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Build Lineage Entities",
            "app_name": "automation-engine",
            "app_task_queue": lineage_task_queue,
            "inputs": {
                "workflow_type": "LineageWorkflow",
                "task_queue": lineage_task_queue,
                "args": {
                    "connection_qualified_name": connection.qualified_name,
                    "connector_name": connector_short_name,
                    "session_key": "view-lineage",
                    "sql_unquoted_case": "lower",
                    "ignore_all_case": False,
                    "input_path": "",
                    "parsed_views_path": "$.extract.outputs.view_lineage_output_prefix",
                    "lineage_output_path": "$.extract.outputs.lineage_stage_prefix",
                    "cache_path": "connection-cache",
                    "file_type": "json",
                    "lake_provider": lake_provider,
                    "cloud_storage_bucket": "$.extract.outputs.storage_bucket",
                },
            },
            "depends_on": {
                "and_conditions": [
                    {"node_id": "qi", "tag": "success"},
                    {"node_id": "publish", "tag": "success"},
                ]
            },
        },
        "lineage-publish": {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Publish Lineage to Atlas",
            "app_name": "publish",
            "app_task_queue": publish_task_queue,
            "inputs": {
                "workflow_type": "PublishWorkflow",
                "task_queue": publish_task_queue,
                "args": {
                    "connection_qualified_name": connection.qualified_name,
                    "transformed_data_prefix": "$.extract.outputs.lineage_stage_prefix",
                    "publish_state_prefix": "$.extract.outputs.lineage_publish_state_prefix",
                    "current_state_prefix": "$.extract.outputs.lineage_current_state_prefix",
                },
            },
            "depends_on": {"node_id": "lineage-app", "tag": "success"},
        },
    }


def build_ae_payload(
    *,
    run_id: int,
    mode: RunMode,
    connector_short_name: str,
    argo_package_name: str,
    argo_template_name: str,
    app_service_url: str,
    connection: ConnectionSpec,
    mustache_subs: MustacheSubstitutions,
    credential_body: CredentialBody | None,
    ae_workflow_slug: str = "",
) -> dict[str, Any]:
    """Assemble the AE submit body.

    Args:
        run_id: Run identifier (typically ``int(time.time())`` or
            ``int(os.environ["GITHUB_RUN_ID"])``). Used as a unique
            tag in the AE workflow name and labels.
        mode: ``DIRECT`` (tier 5) or ``AGENT`` (tier 4).
        connector_short_name: ``mysql``, ``mssql``, ``saperp``, etc.
        argo_package_name: The ``@atlan/<connector>`` Argo package name.
        argo_template_name: Cluster-scoped WorkflowTemplate name.
        app_service_url: HTTP URL the AE workflow can reach the connector at.
        connection: Where the Atlas Connection will be created (drives
            the flat ``connection.*`` parameter rows and metadata).
        mustache_subs: Typed substitution object carrying all
            connector-specific manifest mustache values. Produced by
            ``_mustache_substitutions()`` on the test class and
            serialised via ``model_dump(by_alias=True)`` at the AE
            parameters boundary.
        credential_body: Typed credential body to post as
            ``payload[].body``. SQL connectors return a
            ``<Connector>CredentialBody`` instance; connectors with no
            credentials (public sources) pass ``None`` to omit the
            credential-creation block entirely.
        ae_workflow_slug: If non-empty, used verbatim; otherwise
            auto-derived from connector name + run_id.

    Returns:
        Dict ready to ``orjson.dumps`` and POST to
        ``/api/service/package-workflows?submit=true``.
    """
    label_key = f"orchestration.atlan.com/default-{connector_short_name}-{run_id}"
    ae_workflow_name = f"atlan-{connector_short_name}-{run_id}"
    ae_atlan_name = (
        f"atlan-{connector_short_name}-default-{connector_short_name}-{run_id}"
    )

    # Build parameter list from the typed mustache_subs + universal connection params.
    # The subs model's model_dump(by_alias=True) yields {{...}}-keyed entries; we
    # extract them into flat Argo parameters with the mustache key stripped of braces.
    subs_dict = mustache_subs.model_dump(by_alias=True, mode="json")

    parameters: list[dict[str, Any]] = []

    # Emit each mustache substitution as a named Argo parameter.
    # Keys are "{{name}}" → parameter name is "name" (strip outer braces).
    for alias_key, value in subs_dict.items():
        if not (alias_key.startswith("{{") and alias_key.endswith("}}")):
            continue
        param_name = alias_key[2:-2]  # strip {{ and }}
        if param_name == "connection":
            # Connection goes as JSON string
            parameters.append(
                {
                    "name": "connection",
                    "value": orjson.dumps(value).decode(),
                }
            )
        elif param_name == "agent-json" and value is not None:
            parameters.append(
                {"name": "agent-json", "value": orjson.dumps(value).decode()}
            )
        elif value is not None:
            parameters.append({"name": param_name, "value": value})

    # Flat connection.* parameter rows (UI sends both the nested JSON
    # and these for the orchestrator's convenience).
    attrs = connection.attributes()
    parameters.extend(
        [
            {"name": "connection.name", "value": attrs["name"]},
            {"name": "connection.qualifiedName", "value": attrs["qualifiedName"]},
            {"name": "connection.allowQuery", "value": attrs["allowQuery"]},
            {
                "name": "connection.allowQueryPreview",
                "value": attrs["allowQueryPreview"],
            },
            {"name": "connection.rowLimit", "value": attrs["rowLimit"]},
            {"name": "connection.connectorName", "value": attrs["connectorName"]},
            {"name": "connection.sourceLogo", "value": attrs["sourceLogo"]},
            {"name": "connection.isDiscoverable", "value": attrs["isDiscoverable"]},
            {"name": "connection.isEditable", "value": attrs["isEditable"]},
            {"name": "connection.category", "value": attrs["category"]},
            {
                "name": "connection.adminUsers",
                "value": orjson.dumps(attrs["adminUsers"]).decode(),
            },
            {
                "name": "connection.adminGroups",
                "value": orjson.dumps(attrs["adminGroups"]).decode(),
            },
            {
                "name": "connection.adminRoles",
                "value": orjson.dumps(attrs["adminRoles"]).decode(),
            },
        ]
    )

    result: dict[str, Any] = {
        "metadata": {
            "labels": {
                label_key: "true",
                "orchestration.atlan.com/atlan-ui": "true",
            },
            "annotations": {
                "orchestration.atlan.com/name": f"{connector_short_name.title()} Assets (full-DAG e2e)",
                "package.argoproj.io/name": argo_package_name,
                "orchestration.atlan.com/atlanName": ae_atlan_name,
            },
            "name": ae_workflow_name,
            "namespace": "default",
            "ae_workflow_slug": ae_workflow_slug
            or f"{connector_short_name}-e2e-{run_id}",
            "app_service_url": app_service_url,
        },
        "spec": {
            "templates": [
                {
                    "name": "main",
                    "dag": {
                        "tasks": [
                            {
                                "name": "run",
                                "arguments": {"parameters": parameters},
                                "templateRef": {
                                    "name": argo_template_name,
                                    "template": "main",
                                    "clusterScope": True,
                                },
                            }
                        ]
                    },
                }
            ],
            "entrypoint": "main",
            "workflowMetadata": {
                "annotations": {"package.argoproj.io/name": argo_package_name}
            },
        },
        "execution_mode": "native",
    }

    # ``payload[]`` tells the orchestrator to create a credential and
    # substitute ``{{credentialGuid}}`` in the parameters with the
    # resulting GUID. Omit when the connector has no credential needs
    # (e.g. public-source connectors like openapi Petstore).
    if credential_body is not None:
        result["payload"] = [
            {
                "parameter": "credentialGuid",
                "type": "credential",
                "body": credential_body.model_dump(by_alias=True, mode="json"),
            }
        ]

    return result
