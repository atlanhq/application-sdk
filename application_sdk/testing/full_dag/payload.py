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

    def attributes(self) -> dict[str, Any]:
        """Pyatlan-style ``Connection.attributes`` block."""
        return {
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "allowQuery": self.allow_query,
            "allowQueryPreview": self.allow_query_preview,
            "rowLimit": self.row_limit,
            "defaultCredentialGuid": "{{credentialGuid}}",
            "connectorName": self.connector_name,
            "sourceLogo": self.source_logo,
            "isDiscoverable": self.is_discoverable,
            "isEditable": self.is_editable,
            "category": self.category,
            "adminUsers": list(self.admin_users),
            "adminGroups": list(self.admin_groups),
            "adminRoles": list(self.admin_roles),
        }


@dataclass(frozen=True)
class DatabaseSpec:
    """DB connection details for the credential payload."""

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


def build_ae_payload(
    *,
    run_id: int,
    mode: RunMode,
    connector_short_name: str,
    argo_package_name: str,
    argo_template_name: str,
    app_service_url: str,
    connection: ConnectionSpec,
    database: DatabaseSpec,
    include_filter: str = '{"^def$":[".*"]}',
    exclude_filter: str = "{}",
    agent: AgentSpec | None = None,
) -> dict[str, Any]:
    """Assemble the AE submit body.

    Args:
        run_id: Run identifier (typically ``int(time.time())`` or
            ``int(os.environ["GITHUB_RUN_ID"])``). Used as a unique
            tag in the AE workflow name and labels.
        mode: ``DIRECT`` (tier 5) or ``AGENT`` (tier 4).
        connector_short_name: ``mysql``, ``mssql``, ``saperp``, etc.
            Drives label keys + Argo template selection.
        argo_package_name: The ``@atlan/<connector>`` Argo package name.
        argo_template_name: Cluster-scoped WorkflowTemplate name
            (``atlan-mysql``, ``atlan-mssql``).
        app_service_url: HTTP URL the AE workflow can reach the
            connector at. For tier 4 / agent flow this is metadata only
            (Temporal-queue routing handles dispatch). For tier 5 /
            direct flow this is the in-cluster service URL of the prod
            pod (``http://<app>.<app>-app.svc.cluster.local``).
        connection: Where the Atlas Connection will be created.
        database: Real DB the connector will introspect.
        include_filter / exclude_filter: JSON-encoded dict filter
            strings (the tenant orchestrator deserializes these before
            handing to the connector). Defaults match the "all schemas
            in the default catalog" case.
        agent: Required when ``mode == AGENT``. Ignored in DIRECT mode.

    Returns:
        Dict ready to ``orjson.dumps`` and POST to
        ``/api/service/package-workflows?submit=true``.
    """
    if mode is RunMode.AGENT and agent is None:
        raise ValueError("agent mode requires an AgentSpec")

    label_key = f"orchestration.atlan.com/default-{connector_short_name}-{run_id}"
    ae_workflow_name = f"atlan-{connector_short_name}-{run_id}"
    ae_atlan_name = (
        f"atlan-{connector_short_name}-default-{connector_short_name}-{run_id}"
    )

    parameters: list[dict[str, Any]] = []
    parameters.append({"name": "extraction-method", "value": mode.value})
    parameters.append({"name": "credential-guid", "value": "{{credentialGuid}}"})

    if mode is RunMode.AGENT:
        assert agent is not None
        agent_json = {
            "host": database.host,
            "port": database.port,
            "auth-type": database.auth_type,
            "agent-name": agent.agent_name,
            "agent-type": agent.agent_type,
            "key-type": agent.key_type,
            "aws-auth-method": agent.aws_auth_method,
            "azure-auth-method": agent.azure_auth_method,
            # In agent mode the username/password fields are *secret-store
            # keys*, not literal values — the agent's local Dapr
            # secret-store resolves them at workflow time. Callers should
            # pre-populate the secret store with these keys.
            "basic.username": f"{connector_short_name.upper()}_USERNAME",
            "basic.password": f"{connector_short_name.upper()}_PASSWORD",
        }
        parameters.append(
            {"name": "agent-json", "value": orjson.dumps(agent_json).decode()}
        )

    # Connection (full nested JSON string)
    parameters.append(
        {
            "name": "connection",
            "value": orjson.dumps(
                {"attributes": connection.attributes(), "typeName": "Connection"}
            ).decode(),
        }
    )

    # Filters
    parameters.append({"name": "include-filter", "value": include_filter})
    parameters.append({"name": "exclude-filter", "value": exclude_filter})

    # Credential overrides — in DIRECT mode these go straight through to
    # the prod pod; in AGENT mode they're ignored in favour of the
    # agent-json secret-store keys above.
    parameters.append(
        {
            "name": "credential-guid.credential-type",
            "value": database.connector_config_name
            or f"atlan-connectors-{connector_short_name}",
        }
    )
    parameters.append({"name": "credential-guid.port", "value": database.port})
    parameters.append(
        {"name": "credential-guid.auth-type", "value": database.auth_type}
    )
    if mode is RunMode.DIRECT:
        parameters.append({"name": "credential-guid.host", "value": database.host})
        parameters.append(
            {
                "name": "credential-guid.basic.username",
                "value": database.username,
            }
        )
        parameters.append(
            {
                "name": "credential-guid.basic.password",
                "value": database.password,
            }
        )
    else:
        # Agent mode duplicates a subset under agent-json.* — the Argo
        # cluster template reads these flat parameters separately even
        # though it could also unpack agent-json JSON. Reproducing the
        # exact UI shape avoids subtle service-side validation drift.
        assert agent is not None
        parameters.extend(
            [
                {"name": "agent-json.host", "value": database.host},
                {"name": "agent-json.port", "value": database.port},
                {"name": "agent-json.auth-type", "value": database.auth_type},
                {"name": "agent-json.agent-name", "value": agent.agent_name},
                {"name": "agent-json.agent-type", "value": agent.agent_type},
                {"name": "agent-json.key-type", "value": agent.key_type},
                {"name": "agent-json.aws-auth-method", "value": agent.aws_auth_method},
                {
                    "name": "agent-json.azure-auth-method",
                    "value": agent.azure_auth_method,
                },
                {
                    "name": "agent-json.basic.username",
                    "value": f"{connector_short_name.upper()}_USERNAME",
                },
                {
                    "name": "agent-json.basic.password",
                    "value": f"{connector_short_name.upper()}_PASSWORD",
                },
            ]
        )

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

    return {
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
            "ae_workflow_slug": f"{connector_short_name}-e2e-{run_id}",
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
        "payload": [
            {
                "parameter": "credentialGuid",
                "type": "credential",
                "body": {
                    "name": f"default-{connector_short_name}-{run_id}-0",
                    "host": database.host,
                    "port": database.port,
                    "authType": database.auth_type,
                    "username": database.username,
                    "password": database.password,
                    "extra": dict(database.extra),
                    "connectorConfigName": database.connector_config_name
                    or f"atlan-connectors-{connector_short_name}",
                },
            }
        ],
        "execution_mode": "native",
    }
