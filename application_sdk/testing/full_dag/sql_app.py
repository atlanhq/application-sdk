"""Mid-level base class for SQL-app full-DAG e2e tests.

Sits between :class:`BaseFullDAGE2ETest` and the per-connector test
classes, capturing the boilerplate every SQL connector needs:

* :meth:`agent_spec` — derives a unique-per-run agent name from
  ``connector_short_name`` + ``connection_name_prefix`` + ``run_id``,
  so each CI run gets its own Temporal queue.
* :meth:`connection_spec` — resolves the tenant's ``$admin`` role
  GUID via pyatlan's ``role_cache`` so the API-key service account
  ends up on the Connection's admin ACL. Without this every back-
  side probe ATLAS-403-00-001s.

Subclasses provide:

* The identity attrs (``connector_short_name``, ``argo_package_name``,
  ``argo_template_name``).
* Connector-specific knobs (``include_filter``,
  ``qi_input_prefix_field``, ``expected_min_asset_counts``).
* :meth:`database_spec` returning a :class:`DatabaseSpec` — the host /
  port / credentials of the sibling DB the compose overlay brought up.

Most concrete connector test classes shrink to ~15 lines once they
extend this. The :class:`BaseFullDAGE2ETest` parent remains the
canonical entry point for connector tests that aren't SQL (REST, file-
based, event-driven, etc.) — only the SQL-specific defaults live here.
"""

from __future__ import annotations

import os
from typing import ClassVar

from application_sdk.testing.full_dag.base import BaseFullDAGE2ETest
from application_sdk.testing.full_dag.payload import AgentSpec, ConnectionSpec, RunMode


class SQLAppE2EFullTest(BaseFullDAGE2ETest):
    """Full-DAG e2e harness pre-wired for SQL connectors.

    Most concrete connector tests end up looking like this:

    .. code-block:: python

        class TestMySQLFullDAG(SQLAppE2EFullTest):
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

    Everything else (agent_spec, connection_spec, the $admin role
    resolution) is inherited from this class.
    """

    # Connection ACL — the API-key service account that runs the
    # back-side probe gets onto the Connection via the ``$admin`` role
    # resolved at run time by :meth:`connection_spec`. Override these
    # to add tenant-specific human-debug ACLs if needed; the role
    # path handles the harness-itself case.
    connection_admin_users: ClassVar[tuple[str, ...]] = ()
    connection_admin_groups: ClassVar[tuple[str, ...]] = ()

    # Workflow-agent name template applied when :meth:`agent_spec`
    # runs in AGENT mode. The default formats as
    # ``{connector}-{prefix}-{run_id}`` (matches what the Argo
    # cluster template expects: ``task_queue =
    # atlan-{agent_name}``, with the connector prefix carried on the
    # agent name itself). Override only if your connector's Argo
    # template uses a different naming convention.
    agent_name_template: ClassVar[str] = "{connector}-{prefix}-{run_id}"

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
        """Connection identity with ``$admin`` role on the admin ACL.

        Resolves the tenant's ``$admin`` role GUID at run time so the
        API-key service account (which carries that role by default)
        can read the Connection back via direct entity-fetch — see
        :func:`AEWorkflowClient.connection_exists_in_atlas_via_search`
        for the search-based fallback we use in the poll loop.
        """
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

    @staticmethod
    def _resolve_admin_role_guid() -> str:
        """Look up the tenant's ``$admin`` role GUID via pyatlan.

        Raises a remediation-pointing ``RuntimeError`` if the role
        doesn't exist on the tenant — ``$admin`` is a built-in Atlan
        role so this only fires on a misconfigured tenant.
        """
        from pyatlan.client.atlan import AtlanClient  # noqa: PLC0415

        client = AtlanClient(
            base_url=os.environ["ATLAN_BASE_URL"],
            api_key=os.environ["ATLAN_API_KEY"],
        )
        guid = client.role_cache.get_id_for_name("$admin")
        if guid is None:
            from application_sdk.testing.full_dag._errors import (  # noqa: PLC0415
                AdminRoleNotResolvedError,
            )

            raise AdminRoleNotResolvedError(
                message=(
                    "pyatlan role_cache could not resolve `$admin` role GUID "
                    f"against {os.environ['ATLAN_BASE_URL']} — Connection "
                    "would land with empty adminRoles and the back-side "
                    "direct-fetch probe would 403."
                )
            )
        return guid
