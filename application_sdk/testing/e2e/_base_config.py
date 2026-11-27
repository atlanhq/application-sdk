"""The class attributes of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

The bottom layer of the linear mixin chain ``BaseE2ETest`` is composed from
(FND-4267): config, tenant, teardown, seed, DAG, poll, assets, then ``BaseE2ETest`` itself.
Each layer calls only methods on itself or a layer below it. Subclass
``BaseE2ETest``, never a layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from application_sdk.testing.e2e._manifest_identity import (
    DagNodeIdentity,
)
from application_sdk.testing.e2e.client import AEWorkflowClient
from application_sdk.testing.e2e.payload import (
    RunMode,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.e2e.tenant_pool import TenantPool
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.expectations import (
    AttributeExpectationValue,
)
from application_sdk.testing.harness.teardown import (
    DeleteType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    pass

from application_sdk.testing.e2e._base_types import (
    DAGSpec,
    FullDAGOutcome,
    NodeDispatch,
    QueuePollerReading,
)


class _E2EConfig:
    """Class attributes of ``BaseE2ETest``: subclasses set these."""

    # --- required class attrs (must be overridden) ---------------------
    connector_short_name: ClassVar[str] = ""
    argo_package_name: ClassVar[str] = ""
    argo_template_name: ClassVar[str] = ""
    mode: ClassVar[RunMode] = RunMode.DIRECT
    app_service_url: ClassVar[str] = ""

    # Which of the above setup_method insists on, and which tenant pool the
    # suite may run on (see application_sdk.testing.e2e.tenant_pool). Fixed per
    # base class, not per suite: SystemAppE2ETest sets both.
    _required_class_attrs: ClassVar[tuple[str, ...]] = (
        "connector_short_name",
        "argo_package_name",
        "argo_template_name",
    )
    _tenant_pool: ClassVar[TenantPool] = TenantPool.CONNECTOR

    # --- source-availability tier --------------------------------------
    # Sourcing is the app owner's responsibility. When a connector has NO
    # extraction source provisioned in CI (no free container, and no
    # app-owner-supplied credentials), the full-DAG e2e can't extract
    # anything, so it degrades to a worker-up-only check: assert the app
    # worker deployed and serves /server/health, then stop — no extraction,
    # publish, or Atlas assertions. The full DAG runs only when a source is
    # present. Flipped per-run by the E2E_SOURCE_AVAILABLE env var (set from
    # the sdr-e2e action's `source-available` input); default True so
    # connectors that already have a source run the full DAG unchanged.
    # Plain instance field (not ClassVar): setup_method writes the per-run
    # value to self.source_available, reading this class-level value as its
    # default. A ClassVar annotation would make that instance assignment a
    # type error under mypy/pyright.
    source_available: bool = True
    # Node name -> where the seed DAG routed that node. Populated in
    # _bootstrap_workflow and read only by the stuck-node diagnostic, which
    # degrades to "queue unknown" when it is empty (a suite reusing a
    # pre-existing ae_workflow_slug never builds a seed DAG). Plain instance
    # field, same reason as source_available above; always replaced wholesale,
    # never mutated in place, so the class-level empty default is not shared
    # state.
    _node_dispatch: dict[str, NodeDispatch] = {}
    # Task queue -> what Temporal said was polling it when the poll stopped
    # early. Populated by _capture_stop_point_pollers, which is a no-op unless
    # a Temporal address is configured, and read only by _claim_clause. Empty
    # therefore means "not observed", never "no pollers" — the observed
    # no-pollers answer is a recorded reading whose tuple is empty. Plain
    # instance field, same reason as source_available above.
    _queue_pollers: dict[str, QueuePollerReading] = {}
    # Node name -> the identity the app under test declares for that node,
    # captured from the manifest-derived seed DAG in _bootstrap_workflow and
    # read only by _assert_deployed_manifest_matches. Empty means there is
    # nothing to compare against (a hand-crafted legacy seed DAG, or a suite
    # reusing a pre-existing ae_workflow_slug), and the check self-skips. Plain
    # instance field, same reason as source_available above.
    _expected_node_identities: dict[str, DagNodeIdentity] = {}
    # The deprecated sync AE/Atlas client, built on first access rather than in
    # setup_method — see the `client` property for why lazily. Plain instance
    # field with a class-level default, same reason as source_available above,
    # and the default is what lets the property answer on an instance whose
    # setup_method never ran (or ran on the worker-up-only tier).
    _client: AEWorkflowClient | None = None
    # This run's `$admin` reading, taken at most once — see _admin_identity.
    # Plain instance field with a class-level default, same reason as
    # source_available above.
    _admin_reading: "atlas.Reading[atlas.AdminIdentity] | None" = None
    # AE's version number for the seed version this harness published, or None
    # when the harness published none. _assert_deployed_manifest_matches waits
    # for the published version to differ from this before comparing: while AE
    # still serves the seed, the only DAG on offer is the harness's own, and
    # comparing it to itself would report a match no matter what the tenant
    # runs. Plain instance field, same reason as source_available above.
    _seed_version: int | None = None
    # Health endpoint the worker-up tier probes. The CI worker container
    # serves it on localhost:8000 (the sdr-e2e action gates on the same URL
    # before pytest). Override via E2E_WORKER_HEALTH_URL for other topologies.
    worker_health_url: ClassVar[str] = "http://localhost:8000/server/health"
    worker_health_timeout_seconds: ClassVar[int] = 120
    worker_health_poll_interval_seconds: ClassVar[int] = 3

    # --- tenant-app cold-start budget (FND-402) ------------------------
    # The worker_health_* probe above covers the LOCAL CI worker container
    # (localhost:8000). It does NOT cover the TENANT-installed app pod, which is
    # a different :8000: at AE submit, Heracles POSTs the credential config to
    # http://<conn>.<conn>-app.svc.cluster.local:8000/workflows/v1/config/...
    # against that pod. prepare-tenant installs the app and LM reports the
    # deployment reconciled, but the pod can still be minutes from serving when
    # the leg reaches submit — observed live as intermittent
    # "AE submit failed: HTTP 500 ... dial tcp :8000: connect: connection
    # refused" (s3/gcs/mongodbatlas failed while cloudsql/iceberg passed in the
    # same window). The runner has no kubectl route to the remote tenant
    # vcluster in this path, so the AE submit is the only tenant-facing probe of
    # the pod — there is nothing to gate on except the submit itself.
    #
    # submit_workflow ALREADY retries this: a refused dial arrives as a generic
    # 5xx, which its `retryable` predicate matches. Only its default budget was
    # wrong — 4 x 5s ~= 20s, tuned for transient AE overload, not pod cold
    # start. So run_full_dag re-sizes that existing retry rather than wrapping a
    # second loop around it; on exhaustion submit_workflow raises
    # AppNotReadyError when the last response still read as connection-refused.
    #
    # 300s (5 min): live gcs e2e (run 32259032704) showed the tenant pod cold-
    # starting past the initial 120s window but serving well within 300s, so the
    # AE-submit-500s on gcs/s3/mongodbatlas are a genuine cold-start race, not
    # dead pods. 5 min gives comfortable margin while still failing a genuinely
    # unreachable pod in a bounded time. Connectors may override per-repo; 0
    # restores submit_workflow's own default budget.
    #
    # NOTE: this widens the budget for every retryable submit 5xx, not only the
    # cold-start shape — an AE that is genuinely overloaded now gets the same
    # 5 min to recover. That is deliberate (AE overload is the other thing that
    # fails this leg) and bounded: _RETRY_AFTER_BUDGET_SECONDS still caps the
    # extra waiting a slow origin can request on top of the fixed gap.
    #
    # The headline 300s is the fixed-gap floor, not the true worst case. Each of
    # the retries can additionally honour an origin-requested `retry_after`
    # (bounded by _RETRY_AFTER_BUDGET_SECONDS = 300s per submit call), and every
    # attempt costs its own HTTP round-trip, so a genuinely overloaded origin can
    # stretch the wall-clock bound to ~300s floor + ~300s honoured `retry_after`
    # + per-request time ≈ 10 min. Plan CI timeouts for the ~10 min bound, not
    # the 5 min headline.
    app_ready_timeout_seconds: ClassVar[int] = 300
    app_ready_poll_interval_seconds: ClassVar[int] = 5

    # --- Temporal poller read (opt-in) ---------------------------------
    # The stall guard's NoWorkerOnTaskQueueError is an *inference*: nothing
    # started inside the grace, so probably nothing is polling the extract
    # queue. Temporal can answer that directly, and an empty poller list is the
    # observed form of the same finding — available on the first probe instead
    # of after three minutes of silence. See _observed_pollers.
    #
    # Off by default because the connector CI leg has no route to a tenant's
    # Temporal frontend: the runner cannot reach the tenant vcluster, the same
    # constraint that makes the AE submit the only tenant-facing probe of the
    # installed app pod. Set this (or export E2E_TEMPORAL_ADDRESS) on a suite
    # that does have a route — a local cluster, an in-cluster driver — and the
    # observation is *appended* to the inference rather than replacing it. A
    # read that fails changes nothing: the original diagnostic still stands.
    temporal_address: ClassVar[str] = ""
    temporal_namespace: ClassVar[str] = "default"

    # --- deployed-manifest identity check (FND-129) --------------------
    # Whether to assert, right after submit, that the DAG AE published is the
    # DAG this repo's manifest declares. Verifying the installed *version*
    # (which CI does, via the sdr-e2e action's `expected-app-version`) says the
    # right image is on the tenant; this says the graph that ran is the graph we
    # built — a different claim, because at submit Heracles re-fetches the
    # manifest from the tenant-deployed pod and supersedes the harness's seed
    # version with it.
    #
    # On by default, but it only ever *fails* a leg on a positive finding: an
    # unreadable response, a published version that never superseded the seed,
    # or a connector with no manifest to compare all log and continue. So an
    # unonboarded caller is untouched without opting out, the same way the CI
    # version check self-skips on an empty `expected-app-version`. Set False on
    # a connector whose deployed DAG legitimately diverges from its committed
    # manifest — and say why in the override, because the default position is
    # that such a divergence is the bug this check exists to find.
    assert_deployed_manifest: ClassVar[bool] = True
    # Budget for the published version to supersede the harness's seed. This is
    # AE's own create+publish, server-side and already done by the time submit
    # answers, so the wait is for read-after-write visibility rather than for
    # work: seconds, not minutes. Exhausting it is not a failure — the check
    # reports the supersede as unobserved and the run continues to the poll.
    deployed_manifest_timeout_seconds: ClassVar[int] = 60
    deployed_manifest_poll_interval_seconds: ClassVar[int] = 5

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
    # App-entrypoint for AE's server-side manifest fetch on multi-entrypoint
    # connectors (crawler/miner, extract/lineage, per-flavor). Leave empty to
    # auto-derive from ``manifest_path`` (``.../generated/<ep>/manifest.json`` ->
    # ``<ep>``); set explicitly when ``manifest_path`` is empty (legacy seed DAG)
    # or the entrypoint name differs from the manifest subdir. Empty resolved value
    # => single-entrypoint app, no selector sent (AE fetches the bare manifest).
    entrypoint: ClassVar[str] = ""
    # Ordered DAG runs this suite performs, against ONE seeded connection.
    #
    # Empty (the default) means one run, from the ``entrypoint`` /
    # ``manifest_path`` / expectation ClassVars above — i.e. every suite that
    # existed before this attribute behaves exactly as it did. Declare specs
    # here only when one entrypoint has to run before another *within the same
    # pytest process*, because it consumes something that entrypoint produces:
    #
    # .. code-block:: python
    #
    #     dag_runs = (
    #         DAGSpec(
    #             manifest_path="app/generated/crawler/manifest.json",
    #             expect_lineage=False,
    #             expected_min_asset_counts={"Table": 1},
    #         ),
    #         DAGSpec(),  # this suite's own entrypoint, from the ClassVars
    #     )
    #
    # Each spec is submitted, polled and graded on its own — one
    # :class:`FullDAGOutcome` per run, through :meth:`assert_dag_outcome`, in
    # order. They are never merged into a composite verdict: a crawl and a mine
    # assert different things, and a single "did the suite pass" boolean is
    # exactly the shape that lets one of them stop meaning anything.
    #
    # By default all runs share this suite's minted
    # ``connection_qualified_name`` — that sharing is the point, and it is why
    # this is a list on one suite rather than two ordered CI legs, which would
    # have to move teardown out of ``teardown_method`` (guaranteed on pass, fail
    # AND error) into an ``if: always()`` job a cancelled workflow can still
    # skip, on a leased shared tenant.
    #
    # A run that prepares a *different* connection for a later one to reference
    # — a lineage parent another source owns — names it on
    # ``DAGSpec.connection_qualified_name``. That QN joins the same teardown
    # registry ``seed_assets`` writes to, so however many connections the suite
    # touches, ``teardown_method`` still reclaims all of them.
    #
    # Cost: the runs are serial by nature, so each one adds its own wall clock
    # to the leg (a crawl is minutes, a miner plus its lineage poll is minutes
    # more). Chain a run only when a later one genuinely cannot work without it.
    dag_runs: ClassVar[tuple[DAGSpec, ...]] = ()

    #: One :class:`FullDAGOutcome` per completed run, in run order. Populated by
    #: :meth:`test_full_dag_runs_end_to_end` (a single-element list on a
    #: single-DAG suite), for a suite that wants to assert across runs after
    #: each has been graded on its own. Never reduced to a composite verdict.
    dag_outcomes: list[FullDAGOutcome]
    # Deployment name the tenant's SYSTEM apps (publish, quality, lineage) are
    # registered under, substituted for ``{deployment_name}`` when the harness
    # addresses them. Read via :meth:`resolved_tenant_deployment_name` rather
    # than directly, so a CI run against a tenant that diverges from
    # "production" is an env var on that leg rather than a code change here.
    tenant_deployment_name: ClassVar[str] = "production"
    extract_workflow_type: ClassVar[str] = ""
    # Credential-config name for the ``credential-guid.credential-type`` routing
    # row + the credential body's ``connectorConfigName`` backfill. THE place to
    # declare it — it is what the contract toolkit emits into a bundle's
    # generated ``_e2e_base.py``, so a value set here is the generated identity.
    # Read via :meth:`resolved_connector_config_name`, never directly: that
    # method is the single resolution point, and it also honours (with a
    # deprecation warning) the legacy ``DatabaseSpec.connector_config_name``
    # field for suites that set only that one. Both empty =>
    # build_ae_payload defaults to ``f"atlan-connectors-{connector_short_name}"``.
    connector_config_name: ClassVar[str] = ""
    # Typed substitutions model the harness instantiates for the seed DAG's
    # ``{{...}}`` fills. A connector that declares extra manifest mustache keys
    # only has to point this at its own ``MustacheSubstitutions`` subclass (whose
    # typed fields carry the connector's config defaults) — the base fills the
    # universal fields and the subclass's extra fields fall to their defaults,
    # so no ``_mustache_substitutions()`` override is needed just to add params.
    substitutions_class: ClassVar[type[MustacheSubstitutions]] = MustacheSubstitutions

    # Where a failed run writes its redacted evidence bundle, relative to the
    # pytest working directory. ``results/`` is deliberate rather than arbitrary:
    # it is the directory the shared ``sdr-e2e`` composite already points
    # ``upload-artifact`` at, so a bundle written here becomes a CI artifact with
    # no workflow change and lands beside the container log that action dumps.
    # A suite running outside that action gets a local directory and the same
    # files. Set to "" to write nothing.
    evidence_dir: ClassVar[str] = "results/e2e-evidence"

    ae_poll_interval_seconds: ClassVar[int] = 10
    ae_poll_timeout_seconds: ClassVar[int] = 600
    # Fail-fast stall guard (test-harness only — this module is never imported
    # by the production execution path). If no DAG node has started within this
    # window, run_full_dag raises NoWorkerOnTaskQueueError instead of hanging
    # for the full ae_poll_timeout_seconds. Catches the common wedge where a
    # test's agent_spec().agent_name doesn't match the deployed worker's queue
    # (or a second run_full_dag() in one test targets a different agent_name).
    #
    # On by default at 180s: e2e runs against a dedicated worker (the CI
    # docker-compose container) that long-polls and picks work up within
    # seconds, so a healthy run never trips it, and the full
    # ae_poll_timeout_seconds still bounds real work. Set to 0 to disable on a
    # suite that runs against shared / KEDA-autoscaled infra (e.g. some
    # RunMode.DIRECT setups hitting a prod pod that may be scaled to zero),
    # where legitimate pickup can take longer than any fixed grace.
    #
    # The guard fires at the first poll where elapsed >= grace, so real
    # detection latency is grace + up to one ae_poll_interval_seconds
    # (negligible at the 180/10 defaults; only noticeable if a subclass sets a
    # grace close to the interval).
    ae_stall_grace_seconds: ClassVar[int] = 180
    # DAG-progress watchdog: fail fast if a node has started but no DAG node
    # changes state for this window (a node wedged Running that the one-time
    # ae_stall_grace_seconds latch above cannot catch — e.g. an extract stuck on
    # a slow/failing object-store upload). Set wide enough to clear a
    # legitimately slow single node (lineage on deep queues can sit Running for
    # several minutes) while still turning a hang into a self-terminating
    # failure well before ae_poll_timeout_seconds. 0 disables it.
    #
    # None (the default) derives it from ae_poll_timeout_seconds — see
    # _resolved_progress_stall_seconds. It used to be an absolute 1800s, which
    # silently disabled the watchdog on every suite whose ceiling was 1800s or
    # lower (the poll loop exits before the window can ever close), so those
    # suites burned the full ceiling on a wedge instead of failing at the stall.
    # A relative default cannot be killed by raising the ceiling. Set a positive
    # int to pin the window; setup_method rejects a pinned value that is not
    # strictly below ae_poll_timeout_seconds.
    dag_progress_stall_seconds: ClassVar[int | None] = None
    # ---- Teardown, which runs through the ``connection-delete`` app --------
    #
    # Teardown submits one ``connection-delete`` DAG per connection the run
    # touched (see :mod:`application_sdk.testing.harness.teardown`), so it needs
    # its own two budgets rather than the run's. Both are deliberately smaller
    # than the DAG budgets above: teardown is post-verdict, its failures never
    # red a leg, and a leg that sits in cleanup is a leg holding a CI runner for
    # nothing.
    #
    # Ceiling on one connection's delete. Wide enough for the app to drain a
    # crawl's worth of assets (its own search-and-delete loop is budgeted in
    # tens of thousands per call), narrow enough that a wedged delete does not
    # outlast the run that created it. The DAG-progress watchdog is NOT armed on
    # this poll — connection-delete is a single node that legitimately sits
    # Running while it drains, which a glyph comparison cannot tell from a wedge
    # — so this ceiling is the only bound.
    connection_delete_poll_timeout_seconds: ClassVar[int] = 900
    # How long to wait for the delete node to be picked up before concluding
    # that nothing polls the connection-delete queue, and why it is far shorter
    # than ae_stall_grace_seconds: when nothing does — a scale-to-zero worker
    # that will not wake, or a tenant without the app installed — EVERY
    # connection would otherwise burn the full ceiling above before the pyatlan
    # fallback gets to reclaim the Atlas half. It still has to clear a cold
    # start: connection-delete's atlan.yaml sets ``keda.minReplicaCount: 0``, so
    # its queue is legitimately unpolled between runs and KEDA has to scale it
    # up on queue depth. 0 disables the latch, which means an unpolled queue
    # waits out the ceiling instead.
    connection_delete_stall_grace_seconds: ClassVar[int] = 120
    # How thoroughly teardown deletes. PURGE because that is what the pyatlan
    # purge this replaced did, and an ephemeral connection is never coming back;
    # the app's own default is SOFT, which would leave every run's assets
    # recoverable-and-still-indexed on a shared tenant.
    connection_delete_type: ClassVar[DeleteType] = DeleteType.PURGE
    atlas_poll_interval_seconds: ClassVar[int] = 30
    atlas_poll_timeout_seconds: ClassVar[int] = 1500
    # Probe errors that can never heal by retrying: a deterministic bug in the
    # probe itself (a wrong call signature, a bad config value) raises the same
    # exception on every attempt, so waiting out the timeout only delays the
    # failure. ``seed_connection`` re-raises these immediately.
    _PROBE_NON_TRANSIENT_ERRORS: ClassVar[tuple[type[BaseException], ...]] = (
        TypeError,
        ValueError,
    )
    # Asset counts use a much shorter poll window: Elasticsearch is eventually
    # consistent but assets that will appear do so within seconds of the
    # publish step completing. No point holding CI for 25 minutes.
    atlas_asset_poll_interval_seconds: ClassVar[int] = 5
    atlas_asset_poll_timeout_seconds: ClassVar[int] = 15

    expected_min_asset_counts: ClassVar[dict[str, int]] = {}
    # Exact per-type asset-count parity vs a direct (non-SDR) baseline run.
    # Floors (above) assert ">= N"; this asserts "== N" — catches both
    # under- and over-extraction. Generate the baseline once from a direct
    # run and commit it. Empty = skip exact-count parity.
    expected_exact_counts: ClassVar[dict[str, int]] = {}
    # When True, a run that completes but lands ZERO assets in Atlas fails,
    # backstopping the "workflow COMPLETED but extracted nothing" regression.
    # Fires on any successful run regardless of whether floors/exacts are
    # declared (so it also protects connectors that declare nothing — the ones
    # most likely to silently regress), via the true all-types total count.
    # Skipped only when the connector explicitly opts out
    # (``require_nonempty_assets = False``) or its declared expectations
    # themselves assert zero (only non-positive floors/exacts).
    require_nonempty_assets: ClassVar[bool] = True
    expect_lineage: ClassVar[bool] = True
    # Whether this entrypoint is expected to land a Connection (and assets under
    # it) in Atlas. True for a crawler, whose whole job is to publish a
    # connection's inventory. False for an entrypoint that publishes no assets —
    # a query-history miner, a marker-promotion step — where the pass criterion
    # is the DAG itself plus whatever the suite explicitly declares.
    #
    # When False the Atlas probes (connection poll, per-type counts, lineage) are
    # skipped entirely and ``connection_in_atlas`` drops out of the assertion, so
    # the run is NOT silently graded against expectations it can never meet. It
    # does NOT relax the DAG gate: ``_core_dag_ok`` still decides, so a failing
    # extract still fails the test.
    #
    # A miner that enriches a connection still wants that connection to exist —
    # but it must create it itself, in ``seed_prerequisites()``, under this
    # harness's own ephemeral qualified name so ``teardown_method`` purges it.
    # Never point a suite at a long-lived shared connection: a left-over,
    # half-set-up connection is exactly what greens a later run that should have
    # failed.
    expect_connection: ClassVar[bool] = True

    # Crawler-pipeline nodes that MUST genuinely succeed for the run to count as
    # a pass under the skip-tolerant gate (see ``_core_dag_ok``). Only consulted
    # when ``expect_lineage`` is False: with lineage disabled the AE legitimately
    # Skips the qi + lineage nodes, so requiring every DAG node to reach
    # Succeeded (the strict ``all_nodes_succeeded`` gate) false-fails a crawl
    # whose extract -> publish path fully succeeded. Default is the universal
    # crawler core; connectors whose DAG has an explicit fan-out node can extend
    # it (e.g. ``("branch", "extract", "publish")`` for snowflake). Names are
    # matched EXACTLY against the AE DAG node names — a differently-named node
    # mismatches and fails CLOSED (the gate errors; never a false pass). Override
    # to match a connector whose extract/publish nodes carry other names.
    required_dag_nodes: ClassVar[tuple[str, ...]] = ("extract", "publish")

    # Opt-in: validate the LOCATION (qualifiedName hierarchy) of published
    # assets, not just their counts. Maps typeName -> the number of path
    # segments its qualifiedName must have BELOW the connection QN
    # (for the SQL db>schema>table>column shape: Database=1, Schema=2,
    # Table=3, View=3, Column=4). For each declared type the harness samples a
    # few landed assets and asserts each is nested under the connection at
    # exactly that depth — catching a whole type that published to the wrong
    # hierarchy level (mis-parented / flattened / a dropped path-template
    # segment), which the counts alone can't see. This is the structural
    # complement to the count + non-empty checks for the recurring
    # egress->publish path-drift incident class. Empty = skip (counts +
    # non-empty backstop still run).
    #
    # Scope + contract (so adopters don't over-trust it):
    #   * Systematic-drift detector, NOT per-asset integrity: it samples a few
    #     assets per type (no sort), so it reliably catches "the whole type is
    #     at the wrong depth" but will almost never catch one mis-parented asset
    #     among thousands. That's the right tradeoff for the path-drift class.
    #   * A FULLY-DROPPED type is invisible here (no samples -> the type is
    #     skipped). "Too few / none" is the COUNT check's job — so pair every
    #     type you put here with an expected_min_asset_counts floor for the same
    #     type. (The harness folds these types into its count-poll wait, so the
    #     samples are read AFTER ES indexes them, but a genuinely-zero type is
    #     still only surfaced by the floor.)
    #   * Depth is the number of "/"-separated segments below the connection.
    #     When that reads too deep (a "/" inside a table, schema or database
    #     name), the sampled asset's ancestor qualified names decide instead:
    #     see ``asset_qn_ancestor_attributes``.
    expected_asset_qn_depth: ClassVar[dict[str, int]] = {}

    # The attributes that hold a sampled asset's ancestor qualified names, one
    # entry per level, outermost first; within a level the first attribute the
    # asset carries is read. They are read with the depth samples and consulted
    # only when the "/" count reads too deep, so a "/" inside a name still
    # grades. Default: the SQL hierarchy. A BI or object-store connector whose
    # qualified names embed "/" names its own parent attributes here to use
    # ``expected_asset_qn_depth`` at all.
    asset_qn_ancestor_attributes: ClassVar[tuple[tuple[str, ...], ...]] = (
        atlas.SQL_QN_ANCESTOR_ATTRIBUTES
    )

    # Opt-in: validate the VALUES published assets carry, not just how many
    # landed and where. Maps typeName -> attribute name (as Atlan spells it) ->
    # the claim its value must satisfy. A bare scalar is sugar for
    # ``Exactly(scalar)``; the matcher vocabulary is Exactly / Present / Absent /
    # AtLeast / AtMost, from
    # :mod:`application_sdk.testing.harness.expectations`::
    #
    #     expected_asset_attributes = {
    #         "Schema":   {"tableCount": 8, "viewsCount": 0},
    #         "Database": {"schemaCount": AtLeast(1)},
    #     }
    #
    # Why it exists: the three knobs above are all about SHAPE. A connector can
    # publish a structurally perfect asset tree in which every computed
    # attribute is 0 and every e2e suite in the fleet stays green (FND-2094).
    # Two failure modes are invisible without this:
    #
    #   * a DEGRADED value published as fact — a connector that swallows a
    #     permissions error and substitutes 0 is indistinguishable, in published
    #     output, from a genuinely empty source;
    #   * an attribute that SILENTLY STOPS being set — 0 and absent are the same
    #     number to a count assertion and different states in Atlas.
    #
    # Present-but-zero and absent are therefore kept apart end to end: the
    # reader records which attributes the search payload actually carried, so
    # ``Exactly(0)`` fails on an unset attribute and ``Present()`` is the matcher
    # that catches the silent drop. ``Absent()`` pins the inverse, for a
    # connector whose rule is "leave it unset rather than claim 0".
    #
    # Scope + contract (same shape as expected_asset_qn_depth, and the same
    # reasons — read that attr's docstring too):
    #   * Samples a few assets per type (no sort) and requires EVERY sampled
    #     asset to satisfy every matcher. So a value declared here has to be one
    #     EVERY asset of that type carries. Crawl five schemas, one with no
    #     views and one with ten, and there is no per-type claim about
    #     viewsCount left to make beyond Present() — pin the numbers with
    #     expected_asset_attributes_at below, and keep this knob for the claim
    #     that does hold across the type ("it is set at all").
    #   * A FULLY-DROPPED type is invisible here (no samples -> the type is
    #     skipped). Pair every type you put here with an
    #     expected_min_asset_counts floor for the same type.
    #   * The harness can only see what Atlas INDEXED. An attribute name that is
    #     misspelled, or that the type does not carry, reads as absent — which
    #     is a finding, not a silent pass, but check the spelling before
    #     believing a connector dropped it.
    #
    # Empty = skip (no extra Atlas call).
    expected_asset_attributes: ClassVar[
        dict[str, dict[str, AttributeExpectationValue]]
    ] = {}

    # Opt-in: the same claims about ONE NAMED ASSET rather than about every
    # asset of a type. Maps typeName -> qualifiedName suffix -> attribute ->
    # matcher (a bare scalar is sugar for Exactly)::
    #
    #     expected_asset_attributes_at = {
    #         "Schema": {
    #             "sch_empty": {"viewsCount": 0,  "tableCount": 3},
    #             "sch_busy":  {"viewsCount": 10, "tableCount": 8},
    #         },
    #     }
    #
    # This is the knob that makes a value assertable when it DIFFERS within a
    # type, which is the ordinary case for anything computed per asset. The
    # per-type knob above can only carry a claim the whole type shares.
    #
    # Addressed by SUFFIX because a qualifiedName is not writable: the
    # connection carries a freshly minted epoch, so no class attribute can name
    # an asset under it. The suffix is matched on a path-segment boundary, so
    # "sch" matches ".../db/sch" and never ".../db/other_sch", and a Column can
    # be named "col" rather than "db/sch/tbl/col".
    #
    # Scope + contract:
    #   * A suffix that matches NOTHING is a finding, not a skip — unlike the
    #     per-type sampler, where an empty sample is the count floors' job. The
    #     suite named this asset, so its absence is the claim under test.
    #   * A suffix matching TWO OR MORE assets is also a finding: grading the
    #     first would make the verdict depend on Atlas's result ordering.
    #     Lengthen the suffix until it names exactly one.
    #   * Costs one Atlas search per addressed asset, so it is for the handful
    #     of assets whose values a fixture pins — not for asserting a whole
    #     type asset by asset.
    #
    # Empty = skip (no extra Atlas call).
    expected_asset_attributes_at: ClassVar[
        dict[str, dict[str, dict[str, AttributeExpectationValue]]]
    ] = {}

    # Opt-in: assert WHICH TYPES lineage connects, not only that some lineage
    # exists. Maps "SourceType -> TargetType" -> the minimum number of lineage
    # processes (Process / ColumnProcess / BIProcess, or any other Process
    # subtype) under the connection that take an asset of SourceType as an input
    # and produce an asset of TargetType as an output::
    #
    #     expected_lineage_edges = {
    #         "SigmaDataElementField -> SigmaDataModelColumn": 1,
    #         "Column -> SigmaDataElementField": 1,
    #     }
    #
    # Why it exists: ``expect_lineage`` passes as soon as any asset has lineage,
    # and the counts above cannot see processes at all. A connector that keeps
    # publishing its coarse lineage but stops producing one finer kind — every
    # field-level link, say — lands asset counts identical to a good run and
    # stays green. Naming the edge is what makes that drop a failure.
    #
    # Scope + contract:
    #   * Endpoints are matched by typeName only. A process's inputs or outputs
    #     may be assets of another connection (a warehouse column feeding a BI
    #     field); the PROCESS is what has to sit under this connection.
    #   * Every floor must be >= 1, and a key must be two type names joined by
    #     "->". Anything else is rejected when the run is resolved.
    #   * Read once, after the count poll. A connection with more lineage
    #     processes than the reader's scan cap leaves an unreached floor
    #     ungraded (an Atlas-read error, not a failure) rather than failing on a
    #     partial count.
    #   * Independent of ``expect_lineage``, which only relaxes the DAG gate and
    #     the presence check. A BI connector's transform publishes its
    #     processes even with the lineage nodes skipped.
    #
    # Empty = skip (no extra Atlas call).
    expected_lineage_edges: ClassVar[dict[str, int]] = {}

    # How many assets to sample per type for the attribute check. Separate from
    # the location sampler's fixed 3 because the tradeoff is different: the
    # location check is a systematic-drift detector where three assets prove as
    # much as thirty, while an attribute check may be pinning a value only some
    # assets share. Raising it widens coverage at one search's cost.
    asset_attribute_sample_size: ClassVar[int] = 3

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
