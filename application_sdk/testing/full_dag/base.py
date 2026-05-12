"""Pytest base class for full-DAG e2e tests against tenant system apps.

A connector test:

.. code-block:: python

    import os
    import pytest
    from application_sdk.testing.full_dag import (
        BaseFullDAGE2ETest, RunMode,
    )
    from application_sdk.testing.full_dag.payload import (
        ConnectionSpec, DatabaseSpec, AgentSpec,
    )

    @pytest.mark.full_dag_e2e
    class TestMySQLFullDAG(BaseFullDAGE2ETest):
        connector_short_name = "mysql"
        argo_package_name = "@atlan/mysql"
        argo_template_name = "atlan-mysql"

        # AGENT for tier 4 (CI-side worker); DIRECT for tier 5 (prod pod).
        mode = RunMode.AGENT
        app_service_url = "http://mysql.mysql-app.svc.cluster.local"

        def database_spec(self) -> DatabaseSpec:
            return DatabaseSpec(
                host=os.environ["MYSQL_HOST"],
                port=int(os.environ["MYSQL_PORT"]),
                username=os.environ["MYSQL_USER"],
                password=os.environ["MYSQL_PASSWORD"],
            )

        def agent_spec(self) -> AgentSpec | None:
            return AgentSpec(agent_name=f"ci-{self.run_id}")

The base class handles submit + native-status poll + Atlas-side
Connection assertion + per-node duration reporting. Subclasses just
provide config.

To skip the whole class when the harness env isn't configured (so the
test class can sit alongside per-PR tier-3 tests without breaking
``pytest -q``)::

    if not os.environ.get("ATLAN_BASE_URL"):
        pytest.skip("ATLAN_BASE_URL not set; full-DAG e2e harness disabled",
                    allow_module_level=True)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import ClassVar

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.full_dag.client import AEWorkflowClient, DAGRunResult
from application_sdk.testing.full_dag.payload import (
    AgentSpec,
    ConnectionSpec,
    DatabaseSpec,
    RunMode,
    build_ae_payload,
    build_seed_dag,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class FullDAGOutcome:
    """Combined result of a single full-DAG run.

    Returned by :meth:`BaseFullDAGE2ETest.run_full_dag` so subclasses
    can build their own assertions on top — e.g. "extract took less
    than X seconds", "all four entity types extracted", etc.
    """

    ae_result: DAGRunResult
    connection_qualified_name: str
    connection_in_atlas: bool

    @property
    def succeeded(self) -> bool:
        """True iff every DAG node succeeded *and* Atlas has the Connection."""
        return self.ae_result.all_nodes_succeeded and self.connection_in_atlas


class BaseFullDAGE2ETest:
    """Pytest base — subclass per connector, set class attrs.

    Class attrs subclasses MUST set:

    Attributes:
        connector_short_name: ``mysql``, ``mssql``, ``saperp``, etc.
        argo_package_name: ``@atlan/<connector>``.
        argo_template_name: Cluster-scoped WorkflowTemplate name
            (e.g. ``atlan-mysql``). Must match what the tenant has
            installed.
        mode: :data:`RunMode.AGENT` for tier 4, :data:`RunMode.DIRECT`
            for tier 5.
        app_service_url: HTTP URL the AE workflow's extract activity
            falls back to. Metadata-only in agent mode; used for
            credential injection in direct mode.

    Class attrs with defaults:

    Attributes:
        connection_name_prefix: Prefix applied to the test Connection
            name and qualifiedName so cleanup sweeps can find them.
        include_filter / exclude_filter: JSON-encoded dict-shape
            filter strings (matches the orchestrator's expected wire
            format). Default = "everything under the default catalog".
        connection_admin_users / _groups / _roles: ACL on the test
            Connection. Default empty.
        ae_poll_interval_seconds / ae_poll_timeout_seconds: How
            aggressively to poll native-status.
        atlas_poll_interval_seconds / atlas_poll_timeout_seconds: How
            aggressively to poll the Atlas Connection endpoint.

    Subclass methods to provide config:

        ``database_spec() -> DatabaseSpec``
        ``agent_spec() -> AgentSpec | None``  (None for DIRECT mode)
    """

    # --- required class attrs (must be overridden) ---------------------
    connector_short_name: ClassVar[str] = ""
    argo_package_name: ClassVar[str] = ""
    argo_template_name: ClassVar[str] = ""
    mode: ClassVar[RunMode] = RunMode.DIRECT
    app_service_url: ClassVar[str] = ""

    # --- optional class attrs ------------------------------------------
    # Per-run identifier prefix. Every name the harness builds
    # (Connection QN, AE workflow, credential, agent) embeds this
    # prefix + run_id so cross-system traceability is one grep.
    # Default fits tier-4 e2e-sdr-full; tier-5 e2e-full overrides.
    connection_name_prefix: ClassVar[str] = "e2e-full-ci"
    include_filter: ClassVar[str] = '{"^def$":[".*"]}'
    exclude_filter: ClassVar[str] = "{}"
    connection_admin_users: ClassVar[tuple[str, ...]] = ()
    connection_admin_groups: ClassVar[tuple[str, ...]] = ()
    connection_admin_roles: ClassVar[tuple[str, ...]] = ()
    # When set, the harness uses this slug verbatim and skips the
    # create-workflow / publish-seed-version bootstrap. Useful if the
    # caller has pre-created the workflow via UI / Heracles (matches
    # the tier-5 pattern where the production-deployed pod registered
    # the workflow once on first install). Default empty → harness
    # auto-creates on every run, idempotent on workflow name.
    ae_workflow_slug: ClassVar[str] = ""

    # Workflow name passed to `POST /automation/api/v1/workflows`.
    # When empty, the harness builds a unique-per-run name as
    # ``{connector}-{connection_name_prefix}-{run_id}`` so every CI run
    # creates its own workflow + leaves a clearly-labeled trace in AE
    # rather than reusing a static name (the latter would also work via
    # AE's name-idempotency, but obscures which run produced which
    # version on inspection). Override to a static string if you want
    # a single shared CI workflow.
    ae_workflow_name_override: ClassVar[str] = ""

    # Task queues for the system-app nodes in the seed DAG. Defaults
    # fit devex / prod-style tenants where publish/qi/lineage all
    # listen on the ``-production`` queues. Override for tenants that
    # name queues differently per deployment.
    publish_task_queue: ClassVar[str] = "atlan-publish-production"
    qi_task_queue: ClassVar[str] = "atlan-query-intelligence-production"
    lineage_task_queue: ClassVar[str] = "atlan-lineage-production"
    # mssql ships ``lorien-only`` view-lineage parsing; mysql (per
    # devex sample) uses ``competitive``. Subclasses override to match
    # their manifest.json.
    qi_parsing_mode: ClassVar[str] = "competitive"
    # Override when the connector's worker registers a non-default
    # workflow name. Default = connector_short_name (v3 convention,
    # what mysql uses). Set to e.g. ``"mssql-metadata-extractor"`` for
    # v2-style connectors. Empty string means "use the SDK default".
    extract_workflow_type: ClassVar[str] = ""

    ae_poll_interval_seconds: ClassVar[int] = 10
    ae_poll_timeout_seconds: ClassVar[int] = 600
    atlas_poll_interval_seconds: ClassVar[int] = 30
    atlas_poll_timeout_seconds: ClassVar[int] = 1500

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup_method(self) -> None:
        """Resolve env + build the per-test identity.

        Pytest invokes this before every test method. We re-stamp
        ``run_id`` on each call so re-running the same test class in a
        single pytest invocation never collides on Connection QN.
        """
        for required in (
            "connector_short_name",
            "argo_package_name",
            "argo_template_name",
        ):
            if not getattr(type(self), required, ""):
                raise RuntimeError(
                    f"{type(self).__name__}: class attribute '{required}' must be set"
                )

        tenant_url = os.environ.get("ATLAN_BASE_URL", "").rstrip("/")
        api_token = os.environ.get("ATLAN_API_KEY", "")
        if not tenant_url or not api_token:
            raise RuntimeError(
                "Full-DAG e2e harness requires ATLAN_BASE_URL + ATLAN_API_KEY"
            )

        # Prefer GH-provided run identifier (cross-correlatable with
        # workflow logs) — fall back to seconds-since-epoch.
        gh_run_id = os.environ.get("GITHUB_RUN_ID")
        self.run_id = (
            int(gh_run_id) if gh_run_id and gh_run_id.isdigit() else int(time.time())
        )
        self.client = AEWorkflowClient(tenant_url, api_token)
        self.connection_qualified_name = (
            f"default/{self.connector_short_name}/"
            f"{self.connection_name_prefix}-{self.run_id}"
        )
        self.connection_display_name = (
            f"{self.connection_name_prefix}-{self.connector_short_name}-{self.run_id}"
        )

    # ------------------------------------------------------------------
    # Subclass hooks — override these
    # ------------------------------------------------------------------

    def database_spec(self) -> DatabaseSpec:
        """Real DB the connector will introspect.

        Tier 4 (agent mode): the values you put here go into the AE
        submit payload but credential resolution actually flows through
        the SDR agent's local secret store at run time — so what
        matters here is that ``host`` / ``port`` match what the agent
        can reach.

        Tier 5 (direct mode): values are sent verbatim to the prod pod
        as credential overrides; must work as-is.
        """
        raise NotImplementedError

    def agent_spec(self) -> AgentSpec | None:
        """Agent identity (tier 4 only). Return None for direct mode."""
        if self.mode is RunMode.DIRECT:
            return None
        raise NotImplementedError

    def connection_spec(self) -> ConnectionSpec:
        """Where the resulting Atlas Connection will live.

        Default builds from class attrs + per-run identity. Override if
        you need a different category / icon / ACL per test.
        """
        return ConnectionSpec(
            name=self.connection_display_name,
            qualified_name=self.connection_qualified_name,
            connector_name=self.connector_short_name,
            source_logo=f"https://assets.atlan.com/assets/{self.connector_short_name}.png",
            admin_users=self.connection_admin_users,
            admin_groups=self.connection_admin_groups,
            admin_roles=self.connection_admin_roles,
        )

    # ------------------------------------------------------------------
    # The actual flow
    # ------------------------------------------------------------------

    def _bootstrap_workflow(self) -> str:
        """Ensure an AE workflow exists with a published version.

        Returns the slug to use for the subsequent submit. If
        ``ae_workflow_slug`` is set on the subclass, returns it
        verbatim (caller has pre-bootstrapped). Otherwise:

          1. Create the workflow (idempotent on name).
          2. Publish a seed version with the full 5-node DAG.

        Without a published version, ``POST /api/service/package-
        workflows?submit=true`` returns HTTP 404 ("Workflow with slug
        'X' not found. Create the workflow first.").
        """
        if self.ae_workflow_slug:
            logger.info(
                "Using pre-existing AE workflow slug: %s",
                self.ae_workflow_slug,
            )
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
        # AE has a brief indexing window before the slug is queryable
        # by /versions — sleep 3s here so the first create_version
        # attempt usually succeeds. create_version also retries on
        # 404, so this is belt-and-suspenders for cheap-and-fast.
        time.sleep(3)

        # Derive the extract task_queue from agent_name (tier-4) or
        # the connector's default tenant queue (tier-5). The Argo
        # cluster template builds queue = `atlan-{agent-name}` (the
        # agent-name itself is expected to carry the connector
        # prefix, e.g. `mysql-ci-<run_id>` → `atlan-mysql-ci-<run_id>`)
        # — confirmed against the devex sample (agent-name `mysql-dev`
        # → task_queue `atlan-mysql-dev`). Callers' agent_spec() must
        # produce a name that already includes the connector prefix.
        agent = self.agent_spec()
        if agent is not None:
            extract_queue = f"atlan-{agent.agent_name}"
        else:
            extract_queue = f"atlan-{self.connector_short_name}-default"

        seed_dag = build_seed_dag(
            connector_short_name=self.connector_short_name,
            extract_task_queue=extract_queue,
            publish_task_queue=self.publish_task_queue,
            qi_task_queue=self.qi_task_queue,
            lineage_task_queue=self.lineage_task_queue,
            connection=self.connection_spec(),
            qi_parsing_mode=self.qi_parsing_mode,
            extract_workflow_type=self.extract_workflow_type or None,
            mode=self.mode,
            agent=agent,
            database=self.database_spec(),
        )
        version = self.client.create_version(
            slug,
            {"version": int(time.time()), "dag": seed_dag},
        )
        logger.info("Created seed version %d under slug %s", version, slug)

        self.client.publish_version(slug, version)
        return slug

    def run_full_dag(self) -> FullDAGOutcome:
        """Submit, poll AE, poll Atlas, return the combined outcome.

        Idempotent failures (AE submit returning a recognizable
        "duplicate" error, Atlas already having the QN) are *not*
        special-cased here — the caller is expected to make every
        ``run_id`` unique.

        Returns:
            FullDAGOutcome describing AE node status + Atlas presence.
        """
        slug = self._bootstrap_workflow()

        payload = build_ae_payload(
            run_id=self.run_id,
            mode=self.mode,
            connector_short_name=self.connector_short_name,
            argo_package_name=self.argo_package_name,
            argo_template_name=self.argo_template_name,
            app_service_url=self.app_service_url,
            connection=self.connection_spec(),
            database=self.database_spec(),
            include_filter=self.include_filter,
            exclude_filter=self.exclude_filter,
            agent=self.agent_spec(),
            ae_workflow_slug=slug,
        )

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

        # Only bother probing Atlas if the publish node succeeded —
        # otherwise we waste 25 min waiting for an asset that won't land.
        publish_succeeded = any(
            n.name in {"publish", "publish-stage"} and n.status.is_success
            for n in ae_result.nodes
        )
        if publish_succeeded:
            connection_in_atlas = self.client.poll_atlas_for_connection(
                self.connection_qualified_name,
                interval_seconds=self.atlas_poll_interval_seconds,
                timeout_seconds=self.atlas_poll_timeout_seconds,
            )
        else:
            logger.warning(
                "Publish node did not succeed — skipping Atlas probe to save time"
            )
            connection_in_atlas = False

        return FullDAGOutcome(
            ae_result=ae_result,
            connection_qualified_name=self.connection_qualified_name,
            connection_in_atlas=connection_in_atlas,
        )

    # ------------------------------------------------------------------
    # Default test method
    # ------------------------------------------------------------------

    def test_full_dag_runs_end_to_end(self) -> None:
        """Default pytest method — submit, run, assert success.

        Subclasses can override with their own assertions (entity-count
        thresholds, duration budgets, etc.) — call :meth:`run_full_dag`
        and inspect the returned :class:`FullDAGOutcome`.
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
