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
from dataclasses import dataclass, field
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

    Attributes:
        ae_result: Native-status snapshot from AE for the run.
        connection_qualified_name: QN of the Connection the seed
            DAG would have materialised on success.
        connection_in_atlas: True iff GET /api/meta/entity/
            uniqueAttribute/type/Connection?attr:qualifiedName=...
            returned 200 before the harness gave up.
        asset_counts: Per-typeName counts of descendant assets under
            the Connection QN (Database, Schema, Table, View, Column
            by default). Empty when the Connection probe didn't
            succeed.
        lineage_present: True iff at least one Process / ColumnProcess
            asset exists under the Connection QN. Empty (False) when
            the Connection probe didn't succeed.
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

    # Path to the connector's manifest.json — the canonical source for
    # the system-app DAG shape (per-node args, depends_on, task queue
    # templates). The harness loads this file at bootstrap time and
    # uses it directly as the seed DAG (with placeholder substitution
    # for ``{app_name}`` and ``{deployment_name}``). This keeps the
    # test in lockstep with whatever the connector ships — when the
    # connector adds a new arg or flag to a node, the e2e test picks
    # it up automatically. No more hand-maintained duplicate.
    #
    # Path is resolved relative to the pytest cwd (typically the
    # connector repo root). Set to ``""`` to fall back to the legacy
    # hand-crafted seed produced by :func:`build_seed_dag` — useful
    # for connectors that don't ship a manifest yet.
    manifest_path: ClassVar[str] = "app/generated/manifest.json"

    # Tenant-side ``{deployment_name}`` value substituted into the
    # task queues of the qi / publish / lineage / lineage-publish
    # nodes (everything that's NOT the extract worker). Defaults to
    # ``production`` because the devex tenant's system apps listen on
    # ``atlan-publish-production`` etc. Override for tenants that use
    # a different deployment name.
    tenant_deployment_name: ClassVar[str] = "production"

    # Override when the connector's worker registers a non-default
    # workflow name. Default = connector_short_name (v3 convention,
    # what mysql uses). Set to e.g. ``"mssql-metadata-extractor"`` for
    # v2-style connectors. Empty string means "use the SDK default".
    extract_workflow_type: ClassVar[str] = ""

    # The class attributes below only apply when ``manifest_path`` is
    # unset (i.e. the legacy hand-crafted seed DAG path). With a real
    # manifest in play these are ignored — the manifest carries the
    # canonical values.
    publish_task_queue: ClassVar[str] = "atlan-publish-production"
    qi_task_queue: ClassVar[str] = "atlan-query-intelligence-production"
    lineage_task_queue: ClassVar[str] = "atlan-lineage-production"
    qi_parsing_mode: ClassVar[str] = "competitive"
    qi_input_prefix_field: ClassVar[str] = "view_data_prefix"
    # Override when the connector's worker registers a non-default
    # workflow name. Default = connector_short_name (v3 convention,
    # what mysql uses). Set to e.g. ``"mssql-metadata-extractor"`` for
    # v2-style connectors. Empty string means "use the SDK default".
    extract_workflow_type: ClassVar[str] = ""

    ae_poll_interval_seconds: ClassVar[int] = 10
    ae_poll_timeout_seconds: ClassVar[int] = 600
    atlas_poll_interval_seconds: ClassVar[int] = 30
    atlas_poll_timeout_seconds: ClassVar[int] = 1500

    # Per-typeName minimums for the inventory assertion the default
    # test runs after Connection lands. Keys must be valid Atlas
    # typeNames; values are inclusive lower bounds. Override per
    # connector to encode the hermetic seed dataset's shape — e.g.
    # mysql's seed.sql under include_filter `e2e_main` produces
    # at least: Database=1 (e2e_main), Schema=1, Table=2 (customers
    # + orders), View=1 (v_customer_order_totals), Column=11.
    expected_min_asset_counts: ClassVar[dict[str, int]] = {}

    # When True, the default test asserts that at least one Process /
    # ColumnProcess exists under the Connection — i.e. QI + lineage-
    # app + lineage-publish actually flowed lineage to Atlas, not just
    # reported success at the DAG level. Set False for connectors whose
    # seed dataset has no view definitions to drive lineage from.
    expect_lineage: ClassVar[bool] = True

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
    # Seed DAG — loaded from the connector's manifest.json
    # ------------------------------------------------------------------

    def _seed_dag_from_manifest(self, extract_task_queue: str) -> dict:
        """Load the connector's manifest.json and use it as the seed DAG.

        Reads ``self.manifest_path`` (relative to the test's cwd —
        typically the connector repo root), parses out the ``dag``
        block, and substitutes the placeholders the configurator
        normally fills at deployment time.

        Two substitution passes:

        1. Curly-brace `{...}` (the configurator's static fills):
           ``{app_name}`` → :attr:`connector_short_name`; and
           ``{deployment_name}`` → CI agent queue on ``extract``,
           :attr:`tenant_deployment_name` elsewhere.

        2. Mustache `{{...}}` (the configurator's dynamic fills): these
           map to per-run identities the harness owns — connection,
           agent_json, filters, extraction_method, etc. AE does NOT
           runtime-substitute these in the seed args; they have to be
           concrete by the time the seed version is published or the
           worker receives them as literal placeholder strings and
           hangs.

        ``{{credential-guid}}`` is special-cased: substituted with
        ``{{credentialGuid}}`` (camelCase, the AE-recognised
        Mustache that *is* runtime-substituted from the submit's
        ``payload[].body``).

        Raises:
            RuntimeError: If ``manifest_path`` can't be read or doesn't
                contain a top-level ``dag`` object.
        """
        import json  # noqa: PLC0415 — cold path: only at bootstrap
        from pathlib import Path  # noqa: PLC0415 — cold path: only at bootstrap

        path = Path(self.manifest_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise RuntimeError(
                f"Manifest file not found at {path} — set `manifest_path` on "
                "the test class to the location of the connector's "
                "manifest.json, or set it to '' to fall back to the "
                "hand-crafted seed DAG."
            )
        manifest = json.loads(path.read_text())
        dag = manifest.get("dag")
        if not isinstance(dag, dict) or not dag:
            raise RuntimeError(
                f"Manifest at {path} has no top-level `dag` object — "
                "can't use as a seed DAG."
            )

        def _sub_queue(node_name: str, raw: str) -> str:
            if node_name == "extract":
                # extract goes to the agent / CI worker queue; manifest's
                # `{deployment_name}` template is irrelevant here because
                # CI doesn't use the tenant's deployment identity.
                return extract_task_queue
            return raw.replace("{deployment_name}", self.tenant_deployment_name)

        # Pass 1: curly-brace `{...}` substitutions on the per-node
        # metadata (app_name, task_queue).
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

        # Pass 2: Mustache `{{...}}` substitutions on the per-node args
        # (the dynamic fills the configurator does at deployment time —
        # we do them in-process so the published seed has concrete
        # values the worker can act on).
        mustache_subs = self._mustache_substitutions()
        for name, node in dag.items():
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            args = inputs.get("args")
            if isinstance(args, dict):
                inputs["args"] = self._apply_mustache_subs(args, mustache_subs)

        logger.info(
            "Loaded seed DAG from %s (%d nodes: %s)",
            path,
            len(dag),
            ", ".join(sorted(dag.keys())),
        )
        return dag

    def _mustache_substitutions(self) -> dict:
        """Build the ``{{...}}`` → runtime-value map for seed-DAG fills.

        Acts as the local-CI equivalent of the configurator's
        deployment-time substitution: takes the per-run identities the
        harness owns (connection, agent_json, filter strings, etc.) and
        maps them to the Mustache keys the connector's manifest uses.

        ``{{credential-guid}}`` is the only key we forward to AE rather
        than fill in directly — its value is ``{{credentialGuid}}``
        (camelCase), which AE *does* runtime-substitute from the
        ``payload[].body`` credential it creates at submit time.
        """
        agent = self.agent_spec()
        connection = self.connection_spec()
        database = self.database_spec()

        # Full Connection entity shape — same JSON we attach as the
        # `connection` submit parameter in build_ae_payload.
        connection_entity = {
            "attributes": connection.attributes(),
            "typeName": "Connection",
        }

        # Agent-json shape mirrors what build_seed_dag's hand-crafted
        # extract_args used pre-manifest: AGENT mode → secret-store
        # bundle keys; DIRECT mode → None (worker takes credentials
        # straight from the credential the credential_guid points at).
        if agent is not None:
            agent_json: dict | None = {
                "host": database.host,
                "port": database.port,
                "auth-type": database.auth_type,
                "agent-name": agent.agent_name,
                "agent-type": agent.agent_type,
                "key-type": agent.key_type,
                "aws-auth-method": agent.aws_auth_method,
                "azure-auth-method": agent.azure_auth_method,
                "basic.username": (
                    f"SDR_{self.connector_short_name.upper()}_USERNAME"
                ),
                "basic.password": (
                    f"SDR_{self.connector_short_name.upper()}_PASSWORD"
                ),
            }
        else:
            agent_json = None

        return {
            "{{credential}}": None,
            "{{credential-guid}}": "{{credentialGuid}}",
            "{{connection}}": connection_entity,
            "{{extraction-method}}": self.mode.value,
            "{{agent-json}}": agent_json,
            "{{include-filter}}": self.include_filter,
            "{{exclude-filter}}": self.exclude_filter,
            "{{exclude-table-regex}}": "",
            "{{preflight-check}}": True,
        }

    def _apply_mustache_subs(self, obj, subs: dict):
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

        if self.manifest_path:
            # DRY: load the connector's manifest.json as the seed DAG
            # so the test stays in lockstep with whatever the connector
            # actually ships. Falls back to the hand-crafted seed only
            # if the manifest is missing or the caller explicitly cleared
            # `manifest_path`.
            seed_dag = self._seed_dag_from_manifest(extract_queue)
        else:
            logger.info(
                "manifest_path empty — falling back to hand-crafted build_seed_dag"
            )
            seed_dag = build_seed_dag(
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

        # Only probe Atlas if every DAG node succeeded — extract feeds
        # publish, qi feeds lineage-app, lineage-app feeds lineage-
        # publish, so a partial-success DAG produces a partial-success
        # Connection (or no Connection at all). Polling 25 min for an
        # asset that was never fully materialised wastes CI minutes and
        # buries the real failure under a timeout. Fail fast instead so
        # the assertion message names the failed node.
        asset_counts: dict[str, int] = {}
        lineage_present = False
        if ae_result.all_nodes_succeeded:
            connection_in_atlas = self.client.poll_atlas_for_connection(
                self.connection_qualified_name,
                interval_seconds=self.atlas_poll_interval_seconds,
                timeout_seconds=self.atlas_poll_timeout_seconds,
            )
            if connection_in_atlas:
                # Connection envelope landed — now confirm descendant
                # assets and lineage actually flowed through, not just
                # the empty-connection placeholder. Skipping these
                # would let an extract that returned zero entities pass
                # the e2e (Connection lands either way).
                asset_counts = self.client.count_assets_under_connection(
                    self.connection_qualified_name
                )
                lineage_present = self.client.has_lineage_under_connection(
                    self.connection_qualified_name
                )
                logger.info(
                    "Atlas inventory under %s: %s lineage_present=%s",
                    self.connection_qualified_name,
                    asset_counts,
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
        """Default pytest method — submit, run, assert success.

        Asserts (in order, so the first true failure gets the focus):
          1. Every DAG node succeeded.
          2. The Connection asset exists in Atlas.
          3. Per-type asset counts under the Connection meet the
             subclass-configured ``expected_min_asset_counts`` floor
             (skipped if that dict is empty — back-compat for
             connectors that haven't characterised their seed yet).
          4. At least one Process/ColumnProcess exists under the
             Connection (skipped when ``expect_lineage`` is False).

        Subclasses can override entirely with their own assertions
        (duration budgets, per-entity name checks, etc.) — call
        :meth:`run_full_dag` and inspect the :class:`FullDAGOutcome`.
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

        # --- post-success assertions ---------------------------------
        # Connection landed — now check the descendants did too. A
        # silently empty Connection (zero tables, no lineage) is an
        # easy regression to miss with a Connection-only assertion.
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
                "no lineage rows reached Atlas — likely an issue with "
                "view parsing or the lineage-publish output prefix."
            )
