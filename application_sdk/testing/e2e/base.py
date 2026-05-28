"""Pytest base class for full-DAG e2e tests against tenant system apps.

A connector test:

.. code-block:: python

    import os
    import pytest
    from application_sdk.testing.e2e import BaseE2ETest, RunMode
    from application_sdk.testing.e2e.payload import AgentSpec

    @pytest.mark.e2e
    class TestOpenAPIE2E(OpenAPIGeneratedE2EBase):
        mode = RunMode.AGENT
        connection_name_prefix = "e2e-ci"
        expected_min_asset_counts = {"APISpec": 1, "APIPath": 10}

        def agent_spec(self) -> AgentSpec:
            return AgentSpec(agent_name=f"openapi-e2e-ci-{self.run_id}")

The base class handles submit + native-status poll + Atlas-side
Connection assertion + per-node duration reporting. Subclasses provide
config. SQL connectors subclass
:class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest` instead.

To skip the whole class when the harness env isn't configured::

    if not os.environ.get("ATLAN_BASE_URL"):
        pytest.skip("ATLAN_BASE_URL not set", allow_module_level=True)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from application_sdk.contracts.types import ConnectionRef
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.full_dag._errors import (
    ManifestDagMissingError,
    ManifestFileNotFoundError,
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
)
from application_sdk.testing.full_dag.client import AEWorkflowClient, DAGRunResult
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    RunMode,
    build_ae_payload,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions

logger = get_logger(__name__)


@dataclass(frozen=True)
class FullDAGOutcome:
    """Combined result of a single full-DAG run.

    Returned by :meth:`BaseE2ETest.run_full_dag` so subclasses can build
    their own assertions on top.

    Attributes:
        ae_result: Native-status snapshot from AE for the run.
        connection_qualified_name: QN of the Connection the seed DAG
            would have materialised on success.
        connection_in_atlas: True iff the Connection asset existed in
            Atlas before the harness gave up.
        asset_counts: Per-typeName counts of descendant assets under the
            Connection QN. Empty when the Connection probe didn't succeed.
        lineage_present: True iff at least one Process / ColumnProcess
            asset exists under the Connection QN.
    """

    ae_result: DAGRunResult
    connection_qualified_name: str
    connection_in_atlas: bool
    asset_counts: dict[str, int] = field(default_factory=dict)
    lineage_present: bool = False

    @property
    def succeeded(self) -> bool:
        """True iff every DAG node succeeded *and* Atlas has the Connection."""
        return self.ae_result.all_nodes_succeeded and self.connection_in_atlas


class BaseE2ETest:
    """Pytest base — subclass per connector, set class attrs.

    Source-agnostic: does not assume SQL, REST, file-based, or any other
    connector shape. SQL-specific behaviour lives in
    :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`.

    Class attrs subclasses MUST set:

    Attributes:
        connector_short_name: ``openapi``, ``mysql``, ``mssql``, etc.
        argo_package_name: ``@atlan/<connector>``.
        argo_template_name: Cluster-scoped WorkflowTemplate name.
        mode: :data:`RunMode.AGENT` for tier 4, :data:`RunMode.DIRECT`
            for tier 5.
        app_service_url: HTTP URL the AE workflow's extract activity
            falls back to.

    Class attrs with defaults:

    Attributes:
        connection_type: Atlan catalog type segment used in the Connection
            qualifiedName (``default/<connection_type>/<epoch>``).  When
            empty (the default) the harness falls back to
            ``connector_short_name``.  Override when the two differ — e.g.
            the OpenAPI connector uses ``connector_short_name = "openapi"``
            but its Atlan connection type is ``"api"``
            (``AtlanConnectorType.API.value``).
        connection_admin_users / _groups / _roles: ACL on the Connection.
        ae_poll_interval_seconds / ae_poll_timeout_seconds: AE polling.
        atlas_poll_interval_seconds / atlas_poll_timeout_seconds: Atlas polling.

    Subclass hooks:

        ``agent_spec() -> AgentSpec | None``
        ``connection_spec() -> ConnectionSpec``
        ``_mustache_substitutions() -> MustacheSubstitutions``
        ``_credential_body() -> CredentialBody | None``
    """

    # --- required class attrs (must be overridden) ---------------------
    connector_short_name: ClassVar[str] = ""
    argo_package_name: ClassVar[str] = ""
    argo_template_name: ClassVar[str] = ""
    mode: ClassVar[RunMode] = RunMode.DIRECT
    app_service_url: ClassVar[str] = ""

    # --- optional class attrs ------------------------------------------
    connection_type: ClassVar[str] = ""
    connection_category: ClassVar[str] = "warehouse"
    connection_name_prefix: ClassVar[str] = "e2e-full-ci"
    connection_admin_users: ClassVar[tuple[str, ...]] = ()
    connection_admin_groups: ClassVar[tuple[str, ...]] = ()
    connection_admin_roles: ClassVar[tuple[str, ...]] = ()
    ae_workflow_slug: ClassVar[str] = ""
    ae_workflow_name_override: ClassVar[str] = ""
    manifest_path: ClassVar[str] = "app/generated/manifest.json"
    tenant_deployment_name: ClassVar[str] = "production"
    extract_workflow_type: ClassVar[str] = ""

    ae_poll_interval_seconds: ClassVar[int] = 10
    ae_poll_timeout_seconds: ClassVar[int] = 600
    atlas_poll_interval_seconds: ClassVar[int] = 30
    atlas_poll_timeout_seconds: ClassVar[int] = 1500

    expected_min_asset_counts: ClassVar[dict[str, int]] = {}
    expect_lineage: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup_method(self) -> None:
        """Resolve env + build the per-test identity."""
        for required in (
            "connector_short_name",
            "argo_package_name",
            "argo_template_name",
        ):
            if not getattr(type(self), required, ""):
                raise MissingHarnessClassAttrError(
                    message=f"{type(self).__name__}: class attribute '{required}' must be set",
                    field=required,
                )

        tenant_url = os.environ.get("ATLAN_BASE_URL", "").rstrip("/")
        api_token = os.environ.get("ATLAN_API_KEY", "")
        if not tenant_url or not api_token:
            raise MissingHarnessEnvError(
                message=(
                    "Full-DAG e2e harness requires ATLAN_BASE_URL + ATLAN_API_KEY. "
                    "ATLAN_API_KEY is mandatory because /automation/api/v1/* (AE "
                    "workflow management) requires the realm-admin resource_access "
                    "role that only the API-key's service account carries."
                )
            )

        oauth_client_id = os.environ.get("SDR_OAUTH_CLIENT_ID", "") or os.environ.get(
            "ATLAN_AUTH_CLIENT_ID", ""
        )
        oauth_client_secret = os.environ.get(
            "SDR_OAUTH_CLIENT_SECRET", ""
        ) or os.environ.get("ATLAN_AUTH_CLIENT_SECRET", "")

        gh_run_id = os.environ.get("GITHUB_RUN_ID")
        self.run_id = (
            int(gh_run_id) if gh_run_id and gh_run_id.isdigit() else int(time.time())
        )
        self.client = AEWorkflowClient(
            tenant_url,
            api_token,
            oauth_client_id=oauth_client_id or None,
            oauth_client_secret=oauth_client_secret or None,
        )
        # connection_type overrides connector_short_name when the Atlan
        # catalog type segment differs from the connector's app name (e.g.
        # OpenAPI: connector_short_name="openapi", connection_type="api").
        # The name is pure epoch seconds so Atlas never rejects it for
        # containing hyphens or alpha characters.
        _conn_type = self.connection_type or self.connector_short_name
        _epoch = int(time.time())
        self.connection_qualified_name = f"default/{_conn_type}/{_epoch}"
        self.connection_display_name = f"{_conn_type}-{_epoch}"

    # ------------------------------------------------------------------
    # Subclass hooks — override these
    # ------------------------------------------------------------------

    def agent_spec(self) -> AgentSpec | None:
        """Agent identity (tier 4 only). Return None for direct mode."""
        if self.mode is RunMode.DIRECT:
            return None
        raise NotImplementedError

    def connection_spec(self) -> ConnectionSpec:
        """Where the resulting Atlas Connection will live."""
        # Include the {{credentialGuid}} placeholder only when a credential
        # body will be created — public-source connectors (credential_body=None)
        # must NOT send the literal unsubstituted string to Atlas.
        cred_guid = "{{credentialGuid}}" if self._credential_body() is not None else ""
        return ConnectionSpec(
            name=self.connection_display_name,
            qualified_name=self.connection_qualified_name,
            connector_name=self.connection_type or self.connector_short_name,
            source_logo=f"https://assets.atlan.com/assets/{self.connector_short_name}.png",
            admin_users=self.connection_admin_users,
            admin_groups=self.connection_admin_groups,
            admin_roles=self.connection_admin_roles,
            category=self.connection_category,
            default_credential_guid=cred_guid,
        )

    def _mustache_substitutions(self) -> MustacheSubstitutions:
        """Universal three substitutions every connector needs.

        Subclasses return a connector-specific subclass instance
        (``OpenAPIMustacheSubstitutions``, ``SQLMustacheSubstitutions``, …)
        that carries additional mustache keys. The harness calls
        ``.model_dump(by_alias=True)`` exactly once when seeding the DAG.
        """
        spec = self.connection_spec()
        return MustacheSubstitutions(
            connection=ConnectionRef.model_validate(
                {"typeName": "Connection", "attributes": spec.attributes()}
            ),
        )

    def _credential_body(self) -> CredentialBody | None:
        """Typed credential body posted as ``payload[].body`` to AE.

        Default: None — connector needs no credential body (public
        source). SQL connectors and others that require credentials
        must override to return their codegen'd ``<Connector>CredentialBody``
        instance.
        """
        return None

    def _build_ae_payload(self, slug: str) -> dict[str, Any]:
        """Compose the AE submit payload from typed hook results.

        Subclasses never override this method — they override the two
        typed hooks above.
        """
        return build_ae_payload(
            run_id=self.run_id,
            mode=self.mode,
            connector_short_name=self.connector_short_name,
            argo_package_name=self.argo_package_name,
            argo_template_name=self.argo_template_name,
            app_service_url=self.app_service_url,
            connection=self.connection_spec(),
            mustache_subs=self._mustache_substitutions(),
            credential_body=self._credential_body(),
            ae_workflow_slug=slug,
        )

    def _build_legacy_seed_dag(self, extract_queue: str) -> dict[str, Any]:
        """Build a hand-crafted seed DAG when ``manifest_path`` is unset.

        Base class always raises — only :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`
        overrides this (it has the SQL-specific task-queue and DAG-shape
        knowledge needed). Non-SQL connectors must always ship a
        manifest.json and set ``manifest_path``.
        """
        raise NotImplementedError(
            f"{type(self).__name__}: manifest_path is empty but "
            "_build_legacy_seed_dag() is not overridden. Set manifest_path "
            "to the connector's manifest.json, or override "
            "_build_legacy_seed_dag() if you need a hand-crafted seed DAG."
        )

    # ------------------------------------------------------------------
    # Seed DAG — loaded from the connector's manifest.json
    # ------------------------------------------------------------------

    def _seed_dag_from_manifest(self, extract_task_queue: str) -> dict:
        """Load the connector's manifest.json and use it as the seed DAG."""
        import json
        from pathlib import Path

        path = Path(self.manifest_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ManifestFileNotFoundError(
                message=(
                    f"Manifest file not found at {path} — set `manifest_path` on "
                    "the test class to the location of the connector's "
                    "manifest.json, or set it to '' to fall back to a "
                    "hand-crafted seed DAG."
                ),
                resource_identifier=str(path),
            )
        manifest = json.loads(path.read_text())
        dag = manifest.get("dag")
        if not isinstance(dag, dict) or not dag:
            raise ManifestDagMissingError(
                message=f"Manifest at {path} has no top-level `dag` object — can't use as a seed DAG.",
                location=str(path),
            )

        def _sub_queue(node_name: str, raw: str) -> str:
            if node_name == "extract":
                return extract_task_queue
            return raw.replace("{deployment_name}", self.tenant_deployment_name)

        for name, node in dag.items():
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if isinstance(inputs.get("app_name"), str):
                inputs["app_name"] = inputs["app_name"].replace(
                    "{app_name}", self.connector_short_name
                )
            if isinstance(node.get("app_name"), str):
                node["app_name"] = node["app_name"].replace(
                    "{app_name}", self.connector_short_name
                )
            tq = inputs.get("task_queue")
            if isinstance(tq, str):
                inputs["task_queue"] = _sub_queue(name, tq)

        subs_dict = self._mustache_substitutions().model_dump(by_alias=True)
        for name, node in dag.items():
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            args = inputs.get("args")
            if isinstance(args, dict):
                inputs["args"] = self._apply_mustache_subs(args, subs_dict)

        logger.info(
            "Loaded seed DAG from %s (%d nodes: %s)",
            path,
            len(dag),
            ", ".join(sorted(dag.keys())),
        )
        return dag

    def _apply_mustache_subs(self, obj: Any, subs: dict) -> Any:
        """Recursively replace exact-match ``{{...}}`` strings."""
        if isinstance(obj, dict):
            return {k: self._apply_mustache_subs(v, subs) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._apply_mustache_subs(x, subs) for x in obj]
        if isinstance(obj, str) and obj in subs:
            return subs[obj]
        return obj

    # ------------------------------------------------------------------
    # The actual flow
    # ------------------------------------------------------------------

    def _bootstrap_workflow(self) -> str:
        """Ensure an AE workflow exists with a published version.

        Returns the slug to use for the subsequent submit.
        """
        if self.ae_workflow_slug:
            logger.info("Using pre-existing AE workflow slug: %s", self.ae_workflow_slug)
            return self.ae_workflow_slug

        name = (
            self.ae_workflow_name_override
            or f"{self.connector_short_name}-{self.connection_name_prefix}-{self.run_id}"
        )
        slug = self.client.create_workflow(
            name=name,
            description=f"Full-DAG e2e harness — {self.connector_short_name}",
        )
        logger.info("Created (or reused) AE workflow: name=%s slug=%s", name, slug)
        time.sleep(3)

        agent = self.agent_spec()
        if agent is not None:
            extract_queue = f"atlan-{agent.agent_name}"
        else:
            extract_queue = f"atlan-{self.connector_short_name}-default"

        if self.manifest_path:
            seed_dag = self._seed_dag_from_manifest(extract_queue)
        else:
            logger.info("manifest_path empty — falling back to _build_legacy_seed_dag")
            seed_dag = self._build_legacy_seed_dag(extract_queue)

        version = self.client.create_version(
            slug,
            {"version": int(time.time()), "dag": seed_dag},
        )
        logger.info("Created seed version %d under slug %s", version, slug)
        self.client.publish_version(slug, version)
        return slug

    def run_full_dag(self) -> FullDAGOutcome:
        """Submit, poll AE, poll Atlas, return the combined outcome."""
        slug = self._bootstrap_workflow()
        payload = self._build_ae_payload(slug)

        logger.info(
            "Submitting AE workflow: connector=%s mode=%s qn=%s",
            self.connector_short_name,
            self.mode.value,
            self.connection_qualified_name,
        )
        run_id = self.client.submit_workflow(payload)
        logger.info("AE submit returned run_id=%s", run_id)

        ae_result = self.client.poll_native_status(
            run_id,
            interval_seconds=self.ae_poll_interval_seconds,
            timeout_seconds=self.ae_poll_timeout_seconds,
        )

        asset_counts: dict[str, int] = {}
        lineage_present = False
        if ae_result.all_nodes_succeeded:
            connection_in_atlas = self.client.poll_atlas_for_connection(
                self.connection_qualified_name,
                interval_seconds=self.atlas_poll_interval_seconds,
                timeout_seconds=self.atlas_poll_timeout_seconds,
            )
            if connection_in_atlas:
                asset_counts = self.client.count_assets_under_connection(
                    self.connection_qualified_name
                )
                lineage_counts = self.client.count_lineage_under_connection(
                    self.connection_qualified_name
                )
                lineage_present = any(c > 0 for c in lineage_counts.values())
                logger.info(
                    "Atlas inventory under %s: %s",
                    self.connection_qualified_name,
                    asset_counts,
                )
                logger.info(
                    "Lineage inventory under %s: %s lineage_present=%s",
                    self.connection_qualified_name,
                    lineage_counts,
                    lineage_present,
                )
        else:
            failed_names = ", ".join(n.name for n in ae_result.failed_nodes) or "(none)"
            logger.warning(
                "Skipping Atlas probe — %d/%d DAG nodes did not succeed (failed: %s)",
                len(ae_result.failed_nodes),
                len(ae_result.nodes),
                failed_names,
            )
            connection_in_atlas = False

        return FullDAGOutcome(
            ae_result=ae_result,
            connection_qualified_name=self.connection_qualified_name,
            connection_in_atlas=connection_in_atlas,
            asset_counts=asset_counts,
            lineage_present=lineage_present,
        )

    # ------------------------------------------------------------------
    # Default test method
    # ------------------------------------------------------------------

    def test_full_dag_runs_end_to_end(self) -> None:
        """Submit, run, assert success.

        Asserts (in order):
          1. Every DAG node succeeded.
          2. The Connection asset exists in Atlas.
          3. Per-type asset counts meet ``expected_min_asset_counts`` floors.
          4. At least one Process/ColumnProcess exists (unless ``expect_lineage``
             is False).
        """
        outcome = self.run_full_dag()
        if not outcome.succeeded:
            failed = outcome.ae_result.failed_nodes
            failures_msg = (
                "\n".join(
                    f"  - {n.name}: status={n.status.value} error={n.error_message}"
                    for n in failed
                )
                if failed
                else "  (all DAG nodes succeeded; Connection just didn't land in Atlas)"
            )
            raise AssertionError(
                f"Full-DAG e2e failed for connector={self.connector_short_name}\n"
                f"AE run_id={outcome.ae_result.run_id} "
                f"slug={outcome.ae_result.workflow_slug}\n"
                f"AE status={outcome.ae_result.status.value}\n"
                f"Connection in Atlas? {outcome.connection_in_atlas}\n"
                f"Failed nodes:\n{failures_msg}"
            )

        if self.expected_min_asset_counts:
            shortfalls = [
                f"  - {tn}: got {outcome.asset_counts.get(tn, 0)}, expected >= {floor}"
                for tn, floor in self.expected_min_asset_counts.items()
                if outcome.asset_counts.get(tn, 0) < floor
            ]
            if shortfalls:
                raise AssertionError(
                    "Atlas inventory under "
                    f"{outcome.connection_qualified_name} below thresholds:\n"
                    + "\n".join(shortfalls)
                    + f"\nFull counts: {outcome.asset_counts}"
                )

        if self.expect_lineage and not outcome.lineage_present:
            raise AssertionError(
                "No lineage Process/ColumnProcess assets found under "
                f"{outcome.connection_qualified_name}. The DAG's qi + "
                "lineage-app + lineage-publish nodes reported success but "
                "no lineage rows reached Atlas."
            )
