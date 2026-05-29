"""Mid-level base class for SQL-app full-DAG e2e tests.

Sits between :class:`BaseE2ETest` and the per-connector test classes,
capturing the boilerplate every SQL connector needs:

* SQL-specific class attrs (include/exclude filters, task queues, QI knobs).
* :meth:`agent_spec` — derives a unique-per-run agent name.
* :meth:`connection_spec` — resolves the tenant's ``$admin`` role GUID.
* :meth:`_mustache_substitutions` — builds :class:`SQLMustacheSubstitutions`
  from ``database_spec()`` + ``agent_spec()``.
* :meth:`_build_legacy_seed_dag` — hand-crafted 5-node DAG for connectors
  without a manifest.json.

Subclasses provide:

* The identity attrs (``connector_short_name``, ``argo_package_name``,
  ``argo_template_name``).
* Connector-specific knobs (``include_filter``, ``qi_input_prefix_field``,
  ``expected_min_asset_counts``).
* :meth:`database_spec` — host / port / credentials of the DB under test.
* :meth:`_credential_body` — returns the codegen'd
  ``<Connector>CredentialBody`` instance for the AE payload.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_agent_json,
    build_seed_dag,
)
from application_sdk.testing.e2e.substitutions import SQLMustacheSubstitutions
from application_sdk.testing.full_dag._errors import AdminRoleNotResolvedError


class SQLAppE2ETest(BaseE2ETest):
    """Full-DAG e2e harness pre-wired for SQL connectors.

    Most concrete SQL connector tests look like this:

    .. code-block:: python

        class TestMySQLFullDAG(SQLAppE2ETest):
            connector_short_name = "mysql"
            argo_package_name = "@atlan/mysql"
            argo_template_name = "atlan-mysql"
            mode = RunMode.AGENT
            app_service_url = "http://mysql.mysql-app.svc.cluster.local"

            include_filter = r"^def\\.e2e_main$"
            qi_input_prefix_field = "transformed_data_prefix"
            expected_min_asset_counts = {
                "Database": 1, "Schema": 1, "Table": 2, "View": 1, "Column": 10,
            }

            def database_spec(self) -> DatabaseSpec:
                return DatabaseSpec(
                    host="mysql", port=3306,
                    username="e2e_user", password="e2e_pass",
                    connector_config_name="atlan-connectors-mysql",
                )

            def _credential_body(self):
                db = self.database_spec()
                return MysqlCredentialBody(host=db.host, port=db.port, ...)
    """

    # --- SQL-specific class attrs ------------------------------------
    include_filter: ClassVar[str] = '{"^def$":[".*"]}'
    exclude_filter: ClassVar[str] = "{}"

    # Used only when manifest_path == "" (legacy hand-crafted seed DAG).
    publish_task_queue: ClassVar[str] = "atlan-publish-production"
    qi_task_queue: ClassVar[str] = "atlan-query-intelligence-production"
    lineage_task_queue: ClassVar[str] = "atlan-lineage-production"
    qi_parsing_mode: ClassVar[str] = "competitive"
    qi_input_prefix_field: ClassVar[str] = "view_data_prefix"

    agent_name_template: ClassVar[str] = "{connector}-{prefix}-{run_id}"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def database_spec(self) -> DatabaseSpec:
        """Real DB the connector will introspect.

        Tier 4 (agent mode): values go into the AE submit payload; the
        SDR agent's local secret store handles actual credentials at
        run time. What matters is that ``host`` / ``port`` match what
        the agent can reach.

        Tier 5 (direct mode): values are sent verbatim to the prod pod
        as credential overrides; must work as-is.
        """
        raise NotImplementedError

    def agent_spec(self) -> AgentSpec | None:
        """Default AGENT-mode agent identity, or None in DIRECT mode."""
        if self.mode is RunMode.DIRECT:
            return None
        return AgentSpec(
            agent_name=self.agent_name_template.format(
                connector=self.connector_short_name,
                prefix=self.connection_name_prefix,
                run_id=self.run_id,
            )
        )

    def connection_spec(self) -> ConnectionSpec:
        """Connection identity with ``$admin`` role on the admin ACL."""
        if not hasattr(self, "_admin_role_guid"):
            self._admin_role_guid = self._resolve_admin_role_guid()
        return ConnectionSpec(
            name=self.connection_display_name,
            qualified_name=self.connection_qualified_name,
            connector_name=self.connector_short_name,
            source_logo=(
                f"https://assets.atlan.com/assets/{self.connector_short_name}.png"
            ),
            admin_users=self.connection_admin_users,
            admin_groups=self.connection_admin_groups,
            admin_roles=(self._admin_role_guid,),
        )

    def _mustache_substitutions(self) -> SQLMustacheSubstitutions:
        """Build SQL-flavoured mustache subs from database + agent specs."""
        spec = self.connection_spec()
        connection_ref = ConnectionRef.model_validate(
            {"typeName": "Connection", "attributes": spec.attributes()}
        )
        agent = self.agent_spec()
        database = self.database_spec()

        agent_json: dict[str, Any] | None = (
            build_agent_json(database, agent, self.connector_short_name)
            if agent is not None
            else None
        )

        return SQLMustacheSubstitutions.model_validate(
            {
                "connection": connection_ref,
                "extraction_method": self.mode.value,
                "agent_json": agent_json,
                "include_filter": self.include_filter,
                "exclude_filter": self.exclude_filter,
                "exclude_table_regex": "",
                "preflight_check": True,
            }
        )

    def _credential_body(self) -> CredentialBody | None:
        """SQL connectors must override to return their generated credential body."""
        raise NotImplementedError(
            f"{type(self).__name__}: SQL connectors must override _credential_body() "
            "to return their generated <Connector>CredentialBody instance."
        )

    def _build_legacy_seed_dag(self, extract_queue: str) -> dict[str, Any]:
        """Hand-crafted SQL seed DAG for connectors without a manifest.json."""
        agent = self.agent_spec()
        return build_seed_dag(
            connector_short_name=self.connector_short_name,
            extract_task_queue=extract_queue,
            publish_task_queue=self.publish_task_queue,
            qi_task_queue=self.qi_task_queue,
            lineage_task_queue=self.lineage_task_queue,
            connection=self.connection_spec(),
            include_filter=self.include_filter,
            exclude_filter=self.exclude_filter,
            qi_parsing_mode=self.qi_parsing_mode,
            qi_input_prefix_field=self.qi_input_prefix_field,
            extract_workflow_type=self.extract_workflow_type or None,
            mode=self.mode,
            agent=agent,
            database=self.database_spec(),
        )

    @staticmethod
    def _resolve_admin_role_guid() -> str:
        """Look up the tenant's ``$admin`` role GUID via pyatlan."""
        from pyatlan.client.atlan import AtlanClient  # noqa: PLC0415

        client = AtlanClient(
            base_url=os.environ["ATLAN_BASE_URL"],
            api_key=os.environ["ATLAN_API_KEY"],
        )
        guid = client.role_cache.get_id_for_name("$admin")
        if guid is None:
            raise AdminRoleNotResolvedError(
                message=(
                    "pyatlan role_cache could not resolve `$admin` role GUID "
                    f"against {os.environ['ATLAN_BASE_URL']} — Connection "
                    "would land with empty adminRoles and the back-side "
                    "direct-fetch probe would 403."
                )
            )
        return guid
