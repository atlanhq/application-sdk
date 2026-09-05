"""Pytest base class for full-DAG e2e tests against tenant system apps.

A connector test:

.. code-block:: python

    import os
    import pytest
    from application_sdk.testing.e2e import BaseE2ETest, RunMode

    @pytest.mark.e2e
    class TestOpenAPIE2E(BaseE2ETest):
        mode = RunMode.AGENT
        connection_name_prefix = "e2e-ci"
        expected_min_asset_counts = {"APISpec": 1, "APIPath": 10}

    # agent_spec() is derived from ATLAN_APPLICATION_NAME + ATLAN_DEPLOYMENT_NAME
    # by default (matching the worker's atlan-{app}-{deployment} queue, including
    # any per-leg suffix the CI action sets). Override it only to pin an explicit
    # queue — a run_id-keyed override would silently drop per-leg isolation.

The base class handles submit + native-status poll + Atlas-side
Connection assertion + per-node duration reporting. Subclasses provide
config. SQL connectors subclass
:class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest` instead.

To skip the whole class when the harness env isn't configured::

    if not os.environ.get("ATLAN_BASE_URL"):
        pytest.skip("ATLAN_BASE_URL not set", allow_module_level=True)

**Re-expressed over the harness, in place (child H on FND-224).** Every name,
import path and class attribute here is unchanged — the contract toolkit's
generator is untouched, no ``_e2e_base.py`` moves, and conformance's
``_SDK_HARNESS_BASES`` still names this class — but nothing below implements
plumbing of its own any more. What is left is the *connector policy*: which
queue the extract node goes to, what the seed DAG says, which expectations the
run is graded against. The plumbing it composes is
:mod:`application_sdk.testing.harness`:

* :mod:`~application_sdk.testing.harness.identity` mints the run id and the
  ephemeral connection name — including the qualified name teardown purges,
  which used to come back from ``Connection.creator`` at one-second resolution
  and could collide between two matrix legs;
* :mod:`~application_sdk.testing.harness.starters` publishes the seed version;
* :mod:`~application_sdk.testing.harness.automation_engine` submits and polls;
* :mod:`~application_sdk.testing.harness.atlas` reads Atlas and creates the
  seeded connection;
* :mod:`~application_sdk.testing.harness.expectations` grades the counts and the
  qualified-name depths;
* :mod:`~application_sdk.testing.harness.preconditions` is the worker-health
  probe behind :meth:`BaseE2ETest.assert_worker_up`;
* :mod:`~application_sdk.testing.harness.teardown` purges;
* :mod:`~application_sdk.testing.harness.budgets` carries every timing this
  class' ``ClassVar`` declarations carry.

**Everything below the pytest boundary is async** (FND-224's decision D1). Each
public method here is a one-line
:func:`~application_sdk.testing.harness.bridge.run_sync` shim over an ``_async``
twin that carries the real signature, which is what moves the sync boundary up
to exactly the four methods pytest calls and leaves no synchronous
implementation anywhere underneath. A subclass overriding a public method still
works: the shim is the method it overrides.

**One behaviour changes, deliberately.** An Atlas search that could not be read
used to arrive here as zeros — so an Atlas outage was graded as "the connector
landed no assets" and sent the reader to the wrong team. The harness readers
answer :class:`~application_sdk.testing.harness.outcome.Indeterminate` for that,
and this class now keeps it: an unreadable count raises
:class:`~application_sdk.testing.e2e._errors.AtlasReadIndeterminateError`, which
is not an ``AssertionError``, so pytest reports it as an *error* rather than as a
failure of the thing under test.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import orjson
import pytest
from typing_extensions import deprecated

from application_sdk.common.task_queue import (
    QUEUE_PREFIX,
    application_name_from_env,
    deployment_name_from_env,
    derive_task_queue,
)
from application_sdk.contracts.types import ConnectionRef
from application_sdk.errors.base import safe_traceback
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import delete_prefix
from application_sdk.storage.binding import create_store_from_binding_optional
from application_sdk.testing.e2e._errors import (
    AmbiguousDAGRunError,
    AtlasReadIndeterminateError,
    DAGProgressStalledError,
    DeployedManifestMismatchError,
    HarnessMethodNotImplementedError,
    ManifestDagMissingError,
    ManifestFileNotFoundError,
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    NoWorkerOnTaskQueueError,
    ProgressWatchdogUnreachableError,
    SeededConnectionNotSearchableError,
    WorkerNotHealthyError,
)
from application_sdk.testing.e2e._manifest_identity import (
    DagNodeIdentity,
    compare_node_identities,
    node_identities,
)
from application_sdk.testing.e2e.client import (
    AEWorkflowClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    PublishedVersion,
    cold_start_submit_kwargs,
)
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    RunMode,
    build_ae_payload,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness import seed as harness_seed
from application_sdk.testing.harness._errors import MissingTenantEnvError
from application_sdk.testing.harness.automation_engine import AEClient
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.evidence import (
    EvidenceBundle,
    secrets_from_environment,
    write_bundle,
)
from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    AssetExpectations,
    CountRead,
    Finding,
    SampleRead,
    Unreadable,
    evaluate_counts,
    evaluate_locations,
)
from application_sdk.testing.harness.identity import (
    Minter,
    TenantAuth,
    read_tenant_auth,
)
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    Outcome,
    Settled,
    as_count,
    as_counts,
    as_samples,
)
from application_sdk.testing.harness.preconditions import (
    HealthReading,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.starters import (
    AEWorkflowSpec,
    SubmitRetry,
    publish_seed_version,
)
from application_sdk.testing.harness.teardown import purge_connection
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from obstore.store import ObjectStore
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

# Where the sdr-e2e composite action selects the CI Dapr components, and the
# name the atlan-configurator emits the tenant blobstorage binding under. Both
# are the CI convention rather than a rule, which is why each has an env
# override (``E2E_SEED_COMPONENTS_DIR`` / ``E2E_SEED_STORE_BINDING``) and the
# whole resolution sits behind an overridable method — a leg whose layout
# differs is a per-leg env var, not an edit here.
_DEFAULT_SEED_COMPONENTS_DIR = "ci-deploy/components"
_DEFAULT_SEED_STORE_BINDING = "atlan-objectstore"

# Version that drops the deprecated ``DatabaseSpec.connector_config_name``
# fallback in :meth:`BaseE2ETest.resolved_connector_config_name`. Every
# deprecation in this SDK names its removal version; this is the one for that
# field.
DATABASE_SPEC_CREDENTIAL_TYPE_REMOVAL_VERSION = "4.0"

# Node statuses that are a genuine failure and are never tolerated by the
# skip-tolerant DAG gate (see BaseE2ETest._core_dag_ok). Pending/Scheduled are
# NOT here (they are in-flight or an AE-Skipped node downgraded on an older
# service); Skipped/Omitted are intentional non-runs, not failures.
_HARD_FAIL_NODE_STATUSES = frozenset(
    {DAGNodeStatus.FAILED, DAGNodeStatus.ERROR, DAGNodeStatus.CANCELLED}
)

# Shape of the derived dag_progress_stall_seconds window (see
# _derive_progress_stall_seconds). A fraction of the poll ceiling rather than an
# absolute, so raising ae_poll_timeout_seconds widens the watchdog instead of
# silently putting it out of reach.
_PROGRESS_STALL_CEILING_DIVISOR = 3
# Floor: below this, a legitimately slow single node (lineage on a deep queue
# sits Running for minutes with nothing else in the DAG moving) would trip the
# watchdog on a healthy run.
_PROGRESS_STALL_MIN_SECONDS = 300
# Cap: the old absolute default. Past this the watchdog stops being a fail-fast
# guard — a suite with a 3h ceiling does not want to wait an hour to learn its
# DAG is frozen.
_PROGRESS_STALL_MAX_SECONDS = 1800

# How each kind of finding from
# :mod:`application_sdk.testing.harness.expectations` is rendered back into the
# assertion text a connector suite has always seen. Three shapes, because the
# lines were written for three different readers and normalising them would
# change every connector's red-leg output to buy nothing: a count names its type
# with a colon, a location statement reads as a sentence about one asset, and the
# zero-asset backstop is about the run rather than about a type.
#: Consecutive unproductive Atlas probes the Connection poll tolerates. Lifted
#: verbatim from ``poll_atlas_for_connection``'s ``max_not_found_attempts``
#: default, and split by :meth:`BaseE2ETest._atlas_connection_budget` into the
#: two things it was doing at once: the start-grace window (empty searches, which
#: mean the connection never materialised) and the transient-failure streak
#: (unreadable searches, which mean Atlas could not be asked). Same total
#: tolerance, two diagnoses.
_ATLAS_NOT_FOUND_ATTEMPTS = 10

_FINDING_TEMPLATES = {
    "floor": "  - {subject}: {detail}",
    "exact": "  - {subject}: {detail}",
    "nonempty": "  - {detail}",
    "depth": "  - {subject} {detail}",
    "nesting": "  - {subject} {detail}",
    UNREADABLE: "  - {subject}: {detail}",
}


def _render_finding(finding: Finding) -> str:
    """Render one finding as the assertion line connector suites already read.

    Args:
        finding: What was not met, or could not be graded.

    Returns:
        The line, indented to sit under the summary sentence the caller writes.
        An unrecognised expectation falls back to the subject-and-colon shape
        rather than raising: a new expectation kind added to the harness must
        degrade to a readable line here, not take a red leg's message down with
        it.
    """
    template = _FINDING_TEMPLATES.get(finding.expectation, "  - {subject}: {detail}")
    return template.format(subject=finding.subject, detail=finding.detail)


def _derive_progress_stall_seconds(timeout_seconds: int) -> int:
    """Progress-watchdog window to use when a suite pins no explicit value.

    Args:
        timeout_seconds: The suite's ``ae_poll_timeout_seconds``.

    Returns:
        A window strictly below ``timeout_seconds`` (0 when the ceiling itself
        is non-positive). Strictness is the point: a window the poll loop cannot
        reach is a disabled watchdog, which is what an absolute default did to
        every suite whose ceiling was at or under it.
    """
    if timeout_seconds <= 0:
        return 0
    window = min(
        _PROGRESS_STALL_MAX_SECONDS,
        max(
            _PROGRESS_STALL_MIN_SECONDS,
            timeout_seconds // _PROGRESS_STALL_CEILING_DIVISOR,
        ),
    )
    # The floor can exceed a very short ceiling (a 60s smoke suite): keep the
    # window reachable there by halving the ceiling instead. A ceiling of 1s
    # halves to 0 — no positive window is reachable, so the watchdog is off,
    # which is the honest answer for a ceiling that tight.
    return min(window, timeout_seconds // 2)


def _supersedes(published: int | None, seed: int | None) -> bool:
    """Whether *published* provably replaced the harness's *seed* version.

    Provably is the whole point. AE's version number is optional on the wire
    (``_safe_int`` yields ``None`` for a missing or non-numeric one), and
    ``None != seed`` is true — so an inequality test alone reads an unknown
    version as a supersede and goes on to compare a DAG it cannot attribute to
    the tenant. An absent number cannot prove anything, so it answers False.

    ``seed is None`` is the one case where no proof is needed: the harness
    published no seed version, so there is nothing for AE to have superseded
    and whatever it serves is not the harness's own DAG echoed back.

    Args:
        published: Version number AE reports for the published version.
        seed: Version number the harness published, if it published one.

    Returns:
        True only when the comparison that follows is meaningful.
    """
    if seed is None:
        return True
    return published is not None and published != seed


@dataclass(frozen=True, slots=True)
class _SeedProbeReading:
    """One attempt at the representative child write under a seeded connection.

    A reading rather than an exception, which is the load-bearing choice in
    :meth:`BaseE2ETest._retry_seed_probe_async`: while a fresh Connection's
    access policies are still provisioning, a refused write is the *expected*
    answer, not a failed read. Modelling it as a probe error would spend the
    wait's transient-failure streak on the normal case and end the loop long
    before the policies could go live.

    Attributes:
        error: What the write raised, or ``None`` when it succeeded. Retained
            rather than stringified because it is what the caller re-raises when
            the wait never settles — the suite has to see the 403 itself, not a
            harness paraphrase of it.
    """

    error: Exception | None = None

    @property
    def permitted(self) -> bool:
        """Whether the write went through.

        Returns:
            True when the probe did not raise.
        """
        return self.error is None


@dataclass(frozen=True)
class NodeDispatch:
    """Where the seed DAG asked AE to dispatch one node.

    Recorded per node name at bootstrap so a diagnostic can name the task queue
    a stuck node was waiting on. The harness's seed DAG is the only place this
    is knowable locally — ``native-status`` reports statuses, not routing.

    Caveat carried into the rendered message: at submit, Heracles re-fetches the
    manifest from the tenant-deployed pod and *that* DAG executes, so this is
    the routing the harness asked for, not a guarantee of what ran. For the
    system-app queues (publish / lineage / qi) the two agree in practice — they
    are fixed tenant queue names, not per-run values.
    """

    app_name: str
    task_queue: str


@dataclass(frozen=True)
class FullDAGOutcome:
    """Combined result of a single full-DAG run.

    Returned by :meth:`BaseE2ETest.run_full_dag` so subclasses can build
    their own assertions on top.

    Attributes:
        ae_result: Native-status snapshot from AE for the run.
        connection_qualified_name: QN of the Connection the seed DAG
            would have materialised on success.
        connection_in_atlas: True iff the harness OBSERVED the Connection
            asset in Atlas before giving up. False therefore means "not
            observed", which covers both "absent" and "never probed" — the
            latter when the suite sets ``expect_connection = False``, where
            the Atlas probes are skipped and this field is not consulted by
            :meth:`BaseE2ETest.test_full_dag_runs_end_to_end`.
        asset_counts: Per-typeName counts of descendant assets under the
            Connection QN. Empty when the Connection probe didn't succeed.
        lineage_present: True iff at least one Process / ColumnProcess
            asset exists under the Connection QN.
        asset_qn_samples: A few sampled qualifiedNames per type (only for
            the types in ``expected_asset_qn_depth``); used to assert assets
            landed at the correct hierarchy depth. Empty when location
            validation isn't requested or the Connection probe didn't succeed.
    """

    ae_result: DAGRunResult
    connection_qualified_name: str
    connection_in_atlas: bool
    asset_counts: dict[str, int] = field(default_factory=dict)
    total_assets: int = 0
    lineage_present: bool = False
    asset_qn_samples: dict[str, list[str]] = field(default_factory=dict)
    asset_count_reads: Mapping[str, CountRead] = field(default_factory=dict)
    """Per-type counts as the reader answered them, so a type whose search could
    not be read is an
    :class:`~application_sdk.testing.harness.expectations.Unreadable` here rather
    than a zero. This is what the assertion ladder grades; :attr:`asset_counts`
    is the settled-only projection of it, kept at ``dict[str, int]`` because
    connector suites index it and compare the values."""
    total_asset_read: CountRead | None = None
    """The all-types total as the reader answered it, or ``None`` when it was not
    read. :attr:`total_assets` is its settled projection, ``0`` otherwise."""
    asset_qn_reads: Mapping[str, SampleRead] = field(default_factory=dict)
    """Sampled qualified names as the reader answered them. The location check
    used to fail *open* — a failed sample read arrived as an empty list, which is
    also how "this type landed nothing" is spelled, and an empty list is skipped.
    Keeping the distinction here is what closes finding C4 on FND-224."""
    connection_read: Outcome[bool] | None = None
    """The Connection poll's verdict, or ``None`` when it never ran.

    :attr:`connection_in_atlas` is its settled projection, and on its own it
    cannot say *why* it is False. Three verdicts collapse into that boolean and
    they are not the same finding:
    :class:`~application_sdk.testing.harness.outcome.NeverStarted` and
    :class:`~application_sdk.testing.harness.outcome.Expired` mean the
    Connection never materialised — a real claim about the publish path — while
    :class:`~application_sdk.testing.harness.outcome.Indeterminate` means Atlas
    could not be read, which is a claim about Atlas. Keeping the verdict is what
    lets the ladder tell them apart."""
    lineage_read: bool | Unreadable | None = None
    """Whether any lineage asset was observed, or the fact that it could not be
    read; ``None`` when the probe never ran.

    :attr:`lineage_present` is the settled projection, and ``False`` there means
    both "no Process exists" and "the count could not be read" — the same
    fail-closed-but-misattributed shape the asset counts had."""
    connection_expected: bool = True
    """Mirror of the suite's ``expect_connection``, carried here so
    :attr:`succeeded` can tell "the connection is missing" apart from "this
    entrypoint was never going to publish one". Defaults True, so an outcome
    built without it grades exactly as before."""

    @property
    def succeeded(self) -> bool:
        """True iff every DAG node succeeded and the Connection landed.

        The Connection clause is dropped when :attr:`connection_expected` is
        False — an entrypoint that publishes no inventory (a query-history
        miner, a marker promotion) is graded on its DAG alone.
        """
        return self.ae_result.all_nodes_succeeded and (
            self.connection_in_atlas or not self.connection_expected
        )


@dataclass(frozen=True)
class DAGSpec:
    """One entrypoint DAG run, as a suite *declares* it.

    A suite is not bound to one DAG. ``entrypoint`` and ``manifest_path`` are
    still ``ClassVar``\\s, and a suite that declares nothing here runs exactly
    the one DAG they name — but a suite whose entrypoint *consumes an artifact
    another entrypoint produces* has to run that other entrypoint first, and
    there is no way to do that across CI jobs without sharing a connection
    between two independently-torn-down legs. So the number of DAG runs is a
    property of the suite: see :attr:`BaseE2ETest.dag_runs`.

    The motivating case is a query-history miner whose lineage resolution reads
    an entity cache that only a *crawl of the same connection* writes. Seeding
    Atlas through :meth:`BaseE2ETest.seed_connection` cannot produce it — the
    cache is an object-store artifact, and the only thing that writes it is a
    real crawler DAG run.

    Every field is ``None`` by default and ``None`` means *inherit the class
    attribute of the same name*, so a spec declares only what differs. That is
    what keeps a suite's own run identical whether it is expressed as the
    implicit default or as an explicit ``DAGSpec()``.

    Both halves of a run are per-spec, deliberately. Identity (which DAG) is the
    obvious half; the expectations are the load-bearing one, because they decide
    which Atlas probes *run at all* — ``expect_connection`` gates the connection
    poll and every count under it, ``expect_lineage`` gates both the lineage
    count and the DAG gate's strictness, and the three asset maps decide which
    types are counted. A crawl run graded with a miner's expectations would
    therefore not merely be graded leniently: the readings its grading needs
    would never be taken.

    Attributes:
        entrypoint: App-entrypoint for AE's manifest fetch. ``""`` means
            "derive from this spec's ``manifest_path``", the same as the class
            attribute.
        manifest_path: Path to the manifest whose ``dag`` seeds this run.
        expect_connection: Whether this run is expected to land a Connection.
        expect_lineage: Whether this run is expected to land lineage.
        require_nonempty_assets: Whether a run that lands zero assets fails.
        required_dag_nodes: Nodes that must genuinely succeed under the
            skip-tolerant gate.
        expected_min_asset_counts: Per-type floors.
        expected_exact_counts: Per-type exact-count parity.
        expected_asset_qn_depth: Per-type qualifiedName depth below the
            connection.
        connection_qualified_name: The connection this run is submitted and
            graded against. ``None`` — the default, and what every run did
            before FND-1648 — means the suite's own minted connection, which is
            still the right answer whenever the runs are sequenced *because they
            share state on one connection*. Set it when they are sequenced for
            the opposite reason: a run that prepares a **different** connection
            for a later one to reference (a lineage parent another source owns).
            The QN is registered for teardown when this run activates, so a
            connection named here is purged with the rest even if the run that
            was to consume it never got that far.
        label: Short name for this run, used in logs, in the failure evidence
            bundle and — for any run that is not the suite's own default — in
            the AE workflow name, so N runs in one leg stay N distinguishable
            AE workflows. Defaults to the resolved entrypoint, else
            ``"default"``.
    """

    entrypoint: str | None = None
    manifest_path: str | None = None
    expect_connection: bool | None = None
    expect_lineage: bool | None = None
    require_nonempty_assets: bool | None = None
    required_dag_nodes: tuple[str, ...] | None = None
    expected_min_asset_counts: Mapping[str, int] | None = None
    expected_exact_counts: Mapping[str, int] | None = None
    expected_asset_qn_depth: Mapping[str, int] | None = None
    connection_qualified_name: str | None = None
    label: str = ""


@dataclass(frozen=True)
class ResolvedDAG:
    """A :class:`DAGSpec` with every field settled against the class attributes.

    What the harness actually reads during a run. Separate from ``DAGSpec``
    because a declaration is allowed to say nothing (``None`` everywhere) and
    the run is not: every value here is the one this run is submitted and graded
    with, so a diagnostic can print it and a suite's
    :meth:`BaseE2ETest.assert_dag_outcome` override can branch on it without
    re-deriving anything.
    """

    label: str
    entrypoint: str
    manifest_path: str
    expect_connection: bool
    expect_lineage: bool
    require_nonempty_assets: bool
    required_dag_nodes: tuple[str, ...]
    expected_min_asset_counts: Mapping[str, int]
    expected_exact_counts: Mapping[str, int]
    expected_asset_qn_depth: Mapping[str, int]
    connection_qualified_name: str = ""


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
        ``seed_prerequisites() -> None``
        ``assert_dag_outcome(ResolvedDAG, FullDAGOutcome) -> None``
        ``_mustache_substitutions() -> MustacheSubstitutions``
        ``_credential_body() -> CredentialBody | None``

    **A suite is not bound to one DAG.** :attr:`dag_runs` declares an ordered
    list of :class:`DAGSpec` runs — each its own entrypoint, its own AE submit
    and its own graded :class:`FullDAGOutcome` — all against the one connection
    this suite mints and its one ``teardown_method`` purge. Left empty (the
    default) the suite runs the single DAG its class attributes name, exactly as
    every suite did before. See :class:`DAGSpec` for when that is worth doing.
    """

    # --- required class attrs (must be overridden) ---------------------
    connector_short_name: ClassVar[str] = ""
    argo_package_name: ClassVar[str] = ""
    argo_template_name: ClassVar[str] = ""
    mode: ClassVar[RunMode] = RunMode.DIRECT
    app_service_url: ClassVar[str] = ""

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
    #   * Depth is computed by splitting the QN tail on "/", so it assumes
    #     qualifiedName segments don't themselves contain "/". True for SQL
    #     (db>schema>table>column); BI / object-store connectors whose QNs embed
    #     slashes would mis-count — don't enable it there without adjusting.
    expected_asset_qn_depth: ClassVar[dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup_method(self) -> None:
        """Resolve env + build the per-test identity.

        One line, like every other public method on this class: pytest calls
        this synchronously and never awaits it, so the xunit shape has to stay —
        but nothing under it is synchronous, because resolving the admin ACL is
        an Atlas read (FND-224's decision D1). The same lifecycle is also
        published as async fixtures for a suite that inherits nothing; see
        :mod:`application_sdk.testing.harness.fixtures`.
        """
        run_sync(self._setup_method_async())

    async def _setup_method_async(self) -> None:
        """Validate the declaration, mint this run's identity, open the clients.

        Raises:
            MissingHarnessClassAttrError: A required class attribute is unset.
            ProgressWatchdogUnreachableError: The pinned progress-stall window is
                not strictly below the poll ceiling, so the watchdog could never
                fire.
            MissingHarnessEnvError: The environment carries no tenant.
        """
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

        self._node_dispatch = {}
        self._expected_node_identities = {}
        self._seed_version = None
        self._admin_reading = None
        # No run is active until run_full_dag activates one, so everything
        # resolves to the class's own DAG — which is the whole of what a
        # single-DAG suite ever sees.
        self._active_dag = None
        self.dag_outcomes = []
        self._connection_seeded = False
        self._seeded_connection_qns: list[str] = []
        self._seeded_prefixes: list[str] = []
        self._validate_dag_runs()

        # A pinned progress-stall window that is not strictly below the poll
        # ceiling is a disabled watchdog (see ProgressWatchdogUnreachableError).
        # Checked before the source-availability early return: it is a static
        # configuration error, so it should surface on every tier, not only the
        # ones that reach the poll.
        pinned_stall = type(self).dag_progress_stall_seconds
        if (
            pinned_stall is not None
            and pinned_stall > 0
            and pinned_stall >= self.ae_poll_timeout_seconds
        ):
            raise ProgressWatchdogUnreachableError(
                message=(
                    f"{type(self).__name__}: dag_progress_stall_seconds="
                    f"{pinned_stall}s is >= ae_poll_timeout_seconds="
                    f"{self.ae_poll_timeout_seconds}s, so the DAG-progress "
                    "watchdog can never fire — a wedged run will burn the full "
                    "poll ceiling and report the ceiling instead of the stall. "
                    "Leave dag_progress_stall_seconds unset to derive "
                    f"{_derive_progress_stall_seconds(self.ae_poll_timeout_seconds)}s "
                    "from the ceiling, pin a value below the ceiling, or set 0 to "
                    "disable the watchdog deliberately."
                ),
            )

        # Source-availability tier. When no extraction source is provisioned
        # for this connector in CI, degrade to a worker-up-only check (see the
        # class attr + test_full_dag_runs_end_to_end / assert_worker_up) and
        # skip the AE/tenant wiring entirely — the worker-up tier needs neither
        # a tenant nor credentials. E2E_SOURCE_AVAILABLE (from the sdr-e2e
        # action's `source-available` input) overrides the class default.
        # Empty / whitespace-only is treated as UNSET (fall back to the class
        # default), not as False — only an explicit falsey value flips it off,
        # so a blank env var can't silently degrade a source-having connector.
        env_source = os.environ.get("E2E_SOURCE_AVAILABLE")
        self.source_available = (
            type(self).source_available
            if env_source is None or not env_source.strip()
            else env_source.strip().lower() in ("1", "true", "yes")
        )
        if not self.source_available:
            logger.warning(
                "%s: no extraction source provisioned (E2E_SOURCE_AVAILABLE"
                "=false) — the full-DAG e2e degrades to a worker-up-only check; "
                "extraction, publish, and Atlas assertions are skipped. "
                "Provision a source in the app repo (a CI container, or "
                "app-owner-supplied credentials) to run the full DAG.",
                type(self).__name__,
            )
            return

        # ADR-0014 two-store posture (sdr-e2e's `enable-two-store` input,
        # threaded through as this env var) only changes anything on the
        # CI-built worker container, which is only in the extraction path
        # under RunMode.AGENT — the harness submits to that worker's own
        # dynamic queue. Under RunMode.DIRECT extraction runs on the
        # tenant's already-deployed production pod instead, whose storage
        # config this harness never touches, so a missing App.upload()
        # hand-off would not be caught even with two-store enabled here.
        # Warn rather than fail: a DIRECT-mode run is still valid and
        # useful on its own terms, just not a two-store hand-off check.
        if os.environ.get("TWO_STORE") == "true" and self.mode is RunMode.DIRECT:
            logger.warning(
                "two-store mode is enabled but %s runs in RunMode.DIRECT — "
                "extraction happens on the tenant's own production pod, not "
                "the CI worker the two-store env vars were applied to, so a "
                "missing App.upload() hand-off will NOT be caught by this run.",
                type(self).__name__,
            )

        # The stall guard assumes a dedicated worker that picks work up within
        # seconds. Under RunMode.DIRECT extraction runs on the tenant's own
        # deployed pod, which may be KEDA-idle and cold-start slower than the
        # grace — tripping a spurious NoWorkerOnTaskQueueError on an otherwise
        # healthy run. Nudge the operator toward the opt-out rather than fail.
        if self.mode is RunMode.DIRECT and self.ae_stall_grace_seconds > 0:
            logger.warning(
                "%s runs in RunMode.DIRECT with ae_stall_grace_seconds=%ds — "
                "extraction runs on the tenant's own (possibly KEDA-idle) pod, "
                "whose cold-start can exceed the grace and raise a spurious "
                "NoWorkerOnTaskQueueError. Set ae_stall_grace_seconds = 0 on the "
                "test class to disable the stall guard if you see false failures.",
                type(self).__name__,
                self.ae_stall_grace_seconds,
            )

        auth = self._read_tenant_auth()
        self._minter = Minter.from_environment(os.environ)
        self.run_id = self._minter.run_id()

        self._ae = AEClient(auth.base_url, auth.api_key)
        self._client: AEWorkflowClient | None = None

        # connection_type overrides connector_short_name when the Atlan
        # catalog type segment differs from the connector's app name (e.g.
        # OpenAPI: connector_short_name="openapi", connection_type="api").
        #
        # The name is minted rather than derived inline, and the trailing segment
        # is unique per test *instance*: with the e2e matrix each suite runs as a
        # separate parallel job whose setup can land in the same wall-clock
        # second as another leg's, and rapid same-ref pushes overlap too. A
        # shared connection QN would let one leg's teardown purge another's
        # assets and mix Atlas counts. See
        # :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`.
        identity = self._minter.connection_identity(
            self.connection_type or self.connector_short_name
        )
        self.connection_qualified_name = identity.qualified_name
        self.connection_display_name = identity.display_name

        # Atlas requires at least one non-empty admin list on a Connection
        # (ATLAS-400-00-114). When the subclass leaves all three admin attrs
        # unset, resolve the built-in $admin role GUID so any tenant admin
        # can manage the test connection — not just the token under which
        # the workflow runs — plus the current user, so teardown can purge it.
        #
        # Read through the harness' own Atlas client, which prefers the OAuth
        # identity when one is configured. That is the identity every other
        # Atlas call in this run uses, teardown's purge included, so the user
        # written onto the ACL is the user that later has to delete it.
        #
        # The client is opened whether or not the fallback is needed, because
        # _resolve_run_identity is also where a subclass puts its own tenant
        # lookup. Costless when nothing reads through it: pyatlan's async client
        # connects on its first request, not on __aenter__.
        self._auto_admin_roles = ()
        self._auto_admin_users = ()
        async with self._atlas_client() as client:
            await self._resolve_run_identity(client)

    def _validate_dag_runs(self) -> None:
        """Reject a ``dag_runs`` declaration whose runs cannot stay distinct.

        Two static errors, both of which otherwise surface as a confusing
        result rather than as a message:

        * Two runs resolving to the same label but not to the same run. The
          label names the AE workflow (see :meth:`_ae_workflow_name_suffix`), so
          a collision publishes one run's seed DAG over the other's and leaves
          one AE workflow carrying two graphs.
        * Several runs with ``ae_workflow_slug`` pinned. A pinned slug means
          "submit against a workflow this suite does not own, and seed nothing
          over it" — there is exactly one such workflow, so it cannot express N
          different DAGs.

        Raises:
            AmbiguousDAGRunError: Either of the above.
        """
        if len(self.dag_runs) < 2:
            return
        if self.ae_workflow_slug:
            raise AmbiguousDAGRunError(
                message=(
                    f"{type(self).__name__} declares {len(self.dag_runs)} dag_runs "
                    f"and pins ae_workflow_slug={self.ae_workflow_slug!r}. A pinned "
                    "slug is submitted against as-is and never seeded over, so all "
                    "the runs would execute whatever single DAG that workflow "
                    "carries. Drop the pin, or drop the extra runs."
                ),
                field="ae_workflow_slug",
            )
        by_label: dict[str, ResolvedDAG] = {}
        for spec in self.dag_runs:
            resolved = self.resolve_dag(spec)
            clash = by_label.get(resolved.label)
            if clash is not None and clash != resolved:
                raise AmbiguousDAGRunError(
                    message=(
                        f"{type(self).__name__} declares two different dag_runs "
                        f"that both resolve to the label {resolved.label!r}. The "
                        "label names this run's AE workflow, so both would publish "
                        "their seed DAG over one workflow and the AE run list "
                        "could not tell them apart. Set a distinct DAGSpec.label "
                        "on at least one of them."
                    ),
                    field="dag_runs",
                )
            by_label[resolved.label] = resolved

    async def _resolve_run_identity(self, client: AsyncAtlanClient) -> None:
        """Resolve whatever tenant-side identity this suite needs before it runs.

        The extension point for a subclass that needs its own tenant lookup:
        ``SQLAppE2ETest`` overrides it to resolve the ``$admin`` role its
        ``connection_spec`` puts on the ACL, so that lookup shares this client
        and this moment instead of firing lazily from inside a synchronous
        method. Override it, call ``super()``, and read the result off ``self``.

        Never raises here. An unresolvable ``$admin`` is reported and then
        surfaces as the Connection create being rejected, which is where an
        operator can see what the ACL actually was — the behaviour before child
        H, kept deliberately. A subclass whose lookup is load-bearing may raise;
        that is its policy to choose, which is why
        :func:`~application_sdk.testing.harness.atlas.admin_identity` returns a
        reading rather than deciding for both.

        Args:
            client: The open Atlas client, with this run's identity.
        """
        if any(
            [
                self.connection_admin_users,
                self.connection_admin_groups,
                self.connection_admin_roles,
            ]
        ):
            return
        reading = await self._admin_identity(client)
        if isinstance(reading, Settled):
            self._auto_admin_roles = reading.value.roles
            self._auto_admin_users = reading.value.users
            logger.info(
                "resolved connection admin fallback: roles=%s users=%s",
                self._auto_admin_roles,
                self._auto_admin_users,
            )
            return
        logger.warning(
            "could not resolve the $admin role GUID or the current user, so the "
            "connection will be created with whatever admin ACL the suite "
            "declared (none, here) and Atlas may reject it with "
            "ATLAS-400-00-114. Set connection_admin_roles on the test class.",
            exc_info=reading.cause,
        )

    async def _admin_identity(
        self, client: AsyncAtlanClient
    ) -> atlas.Reading[atlas.AdminIdentity]:
        """This run's ``$admin`` reading, taken once and reused.

        Memoised because two callers want the same answer under *different*
        policies: this class degrades to an empty ACL fallback, and
        ``SQLAppE2ETest`` raises. Taking two readings would let the second fail
        after the first succeeded — a transient blip between them turning a
        healthy run into ``AdminRoleNotResolvedError`` — and would double a
        network call whose answer cannot change within one test.

        Args:
            client: The open Atlas client.

        Returns:
            The reading, settled or indeterminate. Not unwrapped: deciding what
            an absent role means is the caller's, which is why
            :func:`~application_sdk.testing.harness.atlas.admin_identity`
            reports rather than decides.
        """
        if self._admin_reading is None:
            self._admin_reading = await atlas.admin_identity(client)
        return self._admin_reading

    def _read_tenant_auth(self) -> TenantAuth:
        """Read the tenant credentials this run uses, and remember them.

        Delegates to
        :func:`~application_sdk.testing.harness.identity.read_tenant_auth` and
        re-raises its leaf as this package's own. The translation is not
        cosmetic: ``MissingHarnessEnvError`` is the error contract connector
        suites and their CI already match on, and a lift is not the place to
        change what a suite catches.

        Returns:
            The credentials.

        Raises:
            MissingHarnessEnvError: ``ATLAN_BASE_URL`` or ``ATLAN_API_KEY`` is
                absent or blank.
        """
        try:
            auth = read_tenant_auth(os.environ)
        except MissingTenantEnvError as missing:
            raise MissingHarnessEnvError(
                message=(
                    "Full-DAG e2e harness requires ATLAN_BASE_URL + "
                    "ATLAN_API_KEY. ATLAN_API_KEY is mandatory because "
                    "/automation/api/v1/* (AE workflow management) requires "
                    "the realm-admin resource_access role that only the "
                    "API-key's service account carries."
                ),
                field=missing.field,
                cause=missing,
            ) from missing
        self._auth = auth
        return auth

    @property
    @deprecated(
        "BaseE2ETest.client is deprecated and will be removed in v4.0. Use "
        "application_sdk.testing.harness.automation_engine.AEClient and "
        "application_sdk.testing.harness.atlas directly, reaching them from "
        "synchronous code through application_sdk.testing.harness.run_sync."
    )
    def client(self) -> AEWorkflowClient:
        """The deprecated synchronous AE/Atlas client, built on first use.

        .. deprecated:: 3.31
            Removed in v4.0. Nothing in this class routes through it any more —
            ``BaseE2ETest`` talks to
            :class:`~application_sdk.testing.harness.automation_engine.AEClient`
            and :mod:`application_sdk.testing.harness.atlas` directly. It is here
            for the connector suites that reach for it themselves, and it shares
            this run's AE pool rather than opening a second one.

        Built lazily, and that is the point rather than an optimisation: the
        class emits a ``DeprecationWarning`` when it is constructed, so eager
        construction would warn on every connector run about a symbol most of
        them never touch — a notice nobody can act on is a notice everybody
        learns to ignore.

        Returns:
            The client, the same instance on every access.

        Raises:
            MissingHarnessEnvError: The run has no tenant. That is the
                worker-up-only tier, where ``setup_method`` deliberately wires
                nothing: there is no AE client because there is nothing to talk
                to. The tier is reachable *with* a tenant configured
                (``E2E_SOURCE_AVAILABLE=false`` on a leg that has credentials),
                so the absent ``_ae`` is what this checks — reading the
                environment would find a perfectly good tenant and then fail on
                a missing attribute instead.
        """
        if self._client is None:
            ae = getattr(self, "_ae", None)
            if ae is None:
                raise MissingHarnessEnvError(
                    message=(
                        f"{type(self).__name__} has no AE client: setup_method "
                        "wired none, which is the worker-up-only tier "
                        "(source_available=False). There is nothing for "
                        "`self.client` to talk to on that tier — it verifies "
                        "the worker's health endpoint and exercises no DAG."
                    ),
                    field="E2E_SOURCE_AVAILABLE",
                )
            auth = (
                self._auth if getattr(self, "_auth", None) else self._read_tenant_auth()
            )
            self._client = AEWorkflowClient(
                auth.base_url,
                auth.api_key,
                oauth_client_id=auth.oauth_client_id,
                oauth_client_secret=auth.oauth_client_secret,
                ae=ae,
            )
        return self._client

    @client.setter
    def client(self, value: AEWorkflowClient) -> None:
        """Adopt a caller-supplied client.

        Kept writable because the attribute was one for as long as it existed,
        and a suite that assigns its own double is entitled to keep working
        through a refactor whose whole premise is that the public surface does
        not move.

        Args:
            value: The client to use from here on.
        """
        self._client = value

    def _atlas_client(self) -> AbstractAsyncContextManager[AsyncAtlanClient]:
        """Open one Atlas client for a batch of reads, with this run's identity.

        Returns:
            The context manager. One client per phase rather than per call is the
            point: the implementation this replaced stood up a fresh client, and
            therefore a fresh TLS handshake, on every poll iteration.
        """
        auth = getattr(self, "_auth", None) or self._read_tenant_auth()
        return atlas.atlas_client(
            auth.base_url,
            auth.api_key,
            oauth_client_id=auth.oauth_client_id,
            oauth_client_secret=auth.oauth_client_secret,
        )

    def teardown_method(self, method: Any) -> None:
        """Purge all assets created by this test run, regardless of outcome.

        Runs after every test method — including on failure and error — so
        ephemeral connections and their descendants don't accumulate on the
        tenant and degrade search performance over time.

        Failures here are logged as warnings, not re-raised, so they never
        mask the real test result.

        Args:
            method: The test method pytest just ran. Unused; part of the xunit
                signature.
        """
        run_sync(self._teardown_method_async(method))

    async def _teardown_method_async(self, method: Any) -> None:
        """Purge this run's connection, then close the clients it opened.

        The purge itself is
        :func:`~application_sdk.testing.harness.teardown.purge_connection`, which
        reports rather than raises: the batching that is a correctness bound
        (``purge_by_guid`` puts one ``guid=`` parameter per asset into one
        DELETE, and httpx refuses a URL whose query exceeds 64 KiB, so an
        unbatched purge deletes *nothing*), the read-everything-before-deleting
        order that offset pagination makes mandatory, and the two independently
        guarded phases all live there now.

        Args:
            method: The test method pytest just ran. Unused; part of the xunit
                signature.
        """
        try:
            await self._purge_this_run()
            await self._purge_seeded_prefixes()
        finally:
            await self._close_clients()

    async def _purge_this_run(self) -> None:
        """Delete every ephemeral connection this run minted, if there are any.

        The run's own connection first, then every lineage-parent connection
        :meth:`seed_assets` registered — in that order because the run's assets
        hold lineage *references* into the seeded skeletons, and purging the
        referrer before the referent is the direction that cannot strand an
        edge. Each purge is independently guarded: one connection that will not
        purge must not orphan the others, for the same reason the two phases
        inside :func:`~application_sdk.testing.harness.teardown.purge_connection`
        are independently guarded.
        """
        conn_qn = getattr(self, "connection_qualified_name", "")
        seeded = tuple(getattr(self, "_seeded_connection_qns", ()))
        for target in (conn_qn, *seeded):
            if not target:
                continue
            try:
                async with self._atlas_client() as client:
                    await purge_connection(client, target)
            # conformance: ignore[E004] teardown boundary — this runs after the assertions have decided the verdict, so a cleanup failure must never replace a real one; it is logged at WARNING with exc_info
            except Exception:
                logger.warning(
                    "e2e cleanup: could not reach the tenant to purge %s — manual "
                    "purge may be needed",
                    target,
                    exc_info=True,
                )

    async def _purge_seeded_prefixes(self) -> None:
        """Delete the object-store prefix each :meth:`seed_assets` call wrote.

        Separate from the connection purge and run *after* it: the entities are
        what a stranded run trips over, the NDJSON is only bytes, and a store the
        harness cannot reach must not stop the connections from being purged.
        Guarded per prefix and report-not-raise, on the same teardown-boundary
        rule as :meth:`_purge_this_run` — cleanup runs after the assertions have
        decided the verdict and must never replace a real one.
        """
        prefixes = tuple(getattr(self, "_seeded_prefixes", ()))
        if not prefixes:
            return
        for prefix in prefixes:
            try:
                deleted = await delete_prefix(prefix, self.seed_object_store())
                logger.info(
                    "e2e cleanup: deleted %d seed object(s) under %s", deleted, prefix
                )
            # conformance: ignore[E004] teardown boundary — see _purge_this_run; a store the harness cannot reach leaves bytes behind, which is strictly less harmful than replacing the run's verdict
            except Exception:
                logger.warning(
                    "e2e cleanup: could not delete the seed prefix %s — manual "
                    "cleanup may be needed",
                    prefix,
                    exc_info=True,
                )

    async def _close_clients(self) -> None:
        """Release the AE pool this test opened, on the loop that opened it.

        The Atlas reads open and close their own client per phase, so there is
        nothing to close for that half.
        """
        ae = getattr(self, "_ae", None)
        if ae is None:
            return
        try:
            await ae.aclose()
        # conformance: ignore[E004] teardown boundary — the same rule as the purge above; a pool that will not close must not become the test's verdict
        except Exception:
            logger.warning(
                "e2e cleanup: the AE connection pool did not close cleanly",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Budgets
    # ------------------------------------------------------------------
    #
    # The ten timing ClassVars above are unchanged and stay the place a
    # connector declares its timings. What changed is that they are no longer
    # read at the call site: each of these builds the
    # :class:`~application_sdk.testing.harness.budgets.Budget` for one wait, so
    # a suite that overrides a ClassVar gets the same effect it always did and
    # the wait itself takes a single typed allowance.

    def _atlas_connection_budget(self) -> Budget:
        """Allowance for the Connection becoming searchable in Atlas.

        Returns:
            The budget. ``start_grace`` is the ten-consecutive-empty-searches cap
            the lifted implementation carried as ``max_not_found_attempts``,
            expressed as the duration it already was — every probe that reached
            that check was an empty search, so it fired on attempt ten, at nine
            intervals elapsed.
        """
        return Budget(
            timeout=timedelta(seconds=self.atlas_poll_timeout_seconds),
            poll_interval=timedelta(seconds=self.atlas_poll_interval_seconds),
            start_grace=timedelta(
                seconds=(_ATLAS_NOT_FOUND_ATTEMPTS - 1)
                * self.atlas_poll_interval_seconds
            ),
            max_transient_failures=_ATLAS_NOT_FOUND_ATTEMPTS,
            # The reader logs its own per-probe line; a heartbeat would double it.
            heartbeat=None,
        )

    def _atlas_counts_budget(self) -> Budget:
        """Allowance for per-type asset counts to settle after publish.

        Returns:
            The budget. Deliberately short: Elasticsearch is eventually
            consistent, but assets that will appear do so within seconds of
            publish completing.
        """
        return Budget(
            timeout=timedelta(seconds=self.atlas_asset_poll_timeout_seconds),
            poll_interval=timedelta(seconds=self.atlas_asset_poll_interval_seconds),
            # The per-poll inventory line already reports progress every
            # iteration; a heartbeat would duplicate it.
            heartbeat=None,
        )

    def _deployed_manifest_budget(self) -> Budget:
        """Allowance for AE to serve a version that supersedes the harness' seed.

        Returns:
            The budget. Seconds rather than minutes: AE's own create-and-publish
            is server-side and already done by the time the submit answers, so
            this waits for read-after-write visibility rather than for work.
        """
        return Budget(
            timeout=timedelta(seconds=self.deployed_manifest_timeout_seconds),
            poll_interval=timedelta(
                seconds=self.deployed_manifest_poll_interval_seconds
            ),
            heartbeat=None,
        )

    def _worker_health_budget(self) -> Budget:
        """Allowance for the local CI worker container to serve ``/server/health``.

        Returns:
            The budget. No ``start_grace``: a refused connection *is* a reading
            here (see
            :class:`~application_sdk.testing.harness.preconditions.HealthReading`),
            so the two diagnoses the grace would split are already distinguished
            by the reading itself.
        """
        return Budget(
            timeout=timedelta(seconds=self.worker_health_timeout_seconds),
            poll_interval=timedelta(seconds=self.worker_health_poll_interval_seconds),
        )

    def _seed_probe_budget(self) -> Budget:
        """Allowance for a freshly seeded connection to accept a child write.

        Returns:
            The budget, on the Atlas poll's own timings — a fresh Connection
            403s child writes until its access policies go live, and there is no
            API that reports when that has happened.
        """
        return Budget(
            timeout=timedelta(seconds=self.atlas_poll_timeout_seconds),
            poll_interval=timedelta(seconds=self.atlas_poll_interval_seconds),
            # No transient allowance, and that is not the loop being strict: the
            # probe never raises at the primitive. A refused write is the
            # *reading* it returns, the same shape ``check_worker_health`` uses,
            # because "not permitted yet" is the expected answer here rather than
            # a failed read — spending an error streak on it would end the wait
            # long before the policies could go live.
            heartbeat=None,
        )

    # ------------------------------------------------------------------
    # Pre-run seeding
    # ------------------------------------------------------------------

    def seed_prerequisites(self) -> None:
        """Set up whatever must exist in Atlas *before* the DAG runs.

        Called by :meth:`test_full_dag_runs_end_to_end` immediately before
        :meth:`run_full_dag`. Default: no-op, so a crawler suite is unaffected.

        Override this on an entrypoint that consumes state instead of creating
        it. A query-history miner enriches a Connection it does not create; run
        bare, it has nothing to enrich. The override creates that Connection —
        via :meth:`seed_connection` — plus any assets the entrypoint expects to
        find, and the run proceeds against them.

        The seeded state MUST live under this test's own
        ``self.connection_qualified_name``, which :meth:`teardown_method` purges
        along with every descendant. That keeps each run isolated: nothing
        survives to green a later run that should have failed. Do not point a
        suite at a long-lived shared connection to dodge the seeding work — a
        half-set-up left-over is precisely the false pass this design prevents.

        This hook writes to Atlas, so it can only seed what pyatlan can write.
        An entrypoint that consumes an *artifact another entrypoint produces* —
        a miner resolving lineage against the entity cache a crawl writes to
        object storage — cannot be seeded from here at all: nothing but a real
        crawler DAG run produces that artifact. Declare the crawl as a run in
        :attr:`dag_runs` instead, and it executes against this same connection
        before the entrypoint that needs it.
        """

    def seed_connection(self, probe: Callable[[], object] | None = None) -> str:
        """Create this test's Connection in Atlas and wait until it is usable.

        Three steps, all of which a seeding suite would otherwise hand-roll:

        1. Create the Connection at **this run's own minted qualified name**,
           under the connector type and admin ACL ``setup_method`` resolved. That
           name is what the AE payload, the Atlas polls and the
           ``teardown_method`` purge all key off, so the connection stays
           ephemeral and still gets torn down.
        2. Poll until the Connection is searchable, so policy provisioning has
           started before anything tries to write beneath it.
        3. If *probe* is given, retry it until it succeeds. A freshly created
           Connection rejects child writes with a 403 until its access policies
           go live, and there is no API that reports when that has happened —
           a successful child write is the only signal. Pass a callable that
           writes the asset your entrypoint needs; asset-type knowledge stays
           in the suite, the retry loop stays here.

        .. note::
           Step 1 used to let ``Connection.creator`` derive the qualified name
           and then *adopt* whatever came back — ``default/<type>/<epoch>``, at
           one-second resolution. Two matrix legs starting in the same second
           therefore shared a connection, and the first to finish purged the
           other's assets. The name now comes from
           :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`
           and does not move, which is also what makes it predictable enough to
           assert on.

        Args:
            probe: Optional zero-arg callable performing a representative child
                write. Retried until it stops raising, bounded by
                ``atlas_poll_timeout_seconds``. Its return value is ignored.
                Errors in ``_PROBE_NON_TRANSIENT_ERRORS`` (``TypeError``,
                ``ValueError``) are re-raised immediately: they indicate a
                deterministic bug in the probe itself, which no retry can heal,
                so probe authors should raise only transient errors (permission
                / not-yet-provisioned) from the callable. An ``async`` callable
                is awaited.

        Returns:
            The Connection's qualified name (also on
            ``self.connection_qualified_name``).

        Raises:
            UnknownConnectorTypeError: ``connection_type`` is not a pyatlan
                ``AtlanConnectorType``.
            SeededConnectionNotSearchableError: Atlas never returned the
                Connection inside ``atlas_poll_timeout_seconds``.
            Exception: whatever *probe* last raised, if it never succeeded
                within the timeout. Propagated rather than swallowed — a suite
                whose seed never became writable must fail, not run against a
                connection it cannot populate.
        """
        return run_sync(self._seed_connection_async(probe))

    async def _seed_connection_async(
        self, probe: Callable[[], object] | None = None
    ) -> str:
        """Create the connection, wait for it to be searchable, then run *probe*.

        Args:
            probe: See :meth:`seed_connection`.

        Returns:
            The Connection's qualified name.

        Raises:
            SeededConnectionNotSearchableError: See :meth:`seed_connection`.
        """
        qualified_name = self.connection_qualified_name
        if getattr(self, "_connection_seeded", False):
            # One connection per suite, however many DAG runs it performs (see
            # ``dag_runs``): the second call is the seeding hook being reached
            # again, not a second connection being asked for. Creating it twice
            # would leave a duplicate that teardown's single purge does not
            # reach. The probe still runs — it is a wait, and a caller that
            # passed one is entitled to it.
            logger.info(
                "e2e seed: connection %s is already seeded — reusing it",
                qualified_name,
            )
            if probe is not None:
                await self._retry_seed_probe_async(probe)
            return qualified_name
        async with self._atlas_client() as client:
            await atlas.create_connection(
                client,
                qualified_name=qualified_name,
                display_name=self.connection_display_name,
                connector_type=self.connection_type or self.connector_short_name,
                admin_users=list(self.connection_admin_users or self._auto_admin_users),
                admin_groups=list(self.connection_admin_groups),
                admin_roles=list(self.connection_admin_roles or self._auto_admin_roles),
            )
            logger.info("e2e seed: created connection %s", qualified_name)

            searchable = await atlas.poll_for_connection(
                client, qualified_name, budget=self._atlas_connection_budget()
            )
            if not (isinstance(searchable, Settled) and searchable.value):
                raise SeededConnectionNotSearchableError(
                    message=(
                        f"Seeded connection {qualified_name} never became "
                        f"searchable within {self.atlas_poll_timeout_seconds}s "
                        f"({type(searchable).__name__}). The DAG would run "
                        "against a connection Atlas cannot see."
                    ),
                    resource=qualified_name,
                    actual_state=f"not returned by Atlas search ({searchable.label})",
                    cause=getattr(searchable, "cause", None),
                )

        self._connection_seeded = True

        if probe is not None:
            await self._retry_seed_probe_async(probe)

        return qualified_name

    async def _retry_seed_probe_async(self, probe: Callable[[], object]) -> None:
        """Retry *probe* until it stops raising, or the Atlas budget runs out.

        The loop is
        :func:`~application_sdk.testing.harness.waiting.poll_until` — the shared
        primitive, reachable now that this method runs on the harness' own event
        loop. Before child H it could not be: ``probe`` is a *synchronous*
        callable a connector suite supplies, the primitive is async by
        construction (decision D1), and reaching it from a synchronous method
        would have meant either blocking the bridge's loop inside a
        suite-supplied write or offloading it to a thread. Moving the sync
        boundary up to :meth:`seed_connection` removed the choice: the probe is
        called on the loop the wait already owns, and nothing else is scheduled
        on it.

        A refused write is a *reading*, not a probe error — the same shape
        :func:`~application_sdk.testing.harness.preconditions.check_worker_health`
        uses, and for the same reason: "not permitted yet" is the expected answer
        here until the connection's policies go live, so spending a
        transient-failure streak on it would end the wait long before it could
        succeed.

        Args:
            probe: The representative child write. Its return value is ignored;
                only whether it raises matters. An ``async`` callable is awaited.

        Raises:
            Exception: Whatever *probe* last raised — immediately for a
                non-transient error, or at the deadline otherwise. Propagated
                rather than swallowed: a suite whose seed never became writable
                must fail, not run against a connection it cannot populate.
        """
        qualified_name = self.connection_qualified_name

        async def _attempt() -> _SeedProbeReading:
            try:
                result = probe()
                if isinstance(result, Awaitable):
                    await result
            except self._PROBE_NON_TRANSIENT_ERRORS:
                # A deterministic probe bug (a TypeError from a wrong call
                # signature, a config ValueError) will fail every retry
                # identically — burning the whole timeout before surfacing.
                # Raising here leaves the wait unclassified, so it propagates.
                logger.error(
                    "e2e seed: probe write under %s raised a non-transient "
                    "error — failing fast rather than retrying until the %ds "
                    "timeout",
                    qualified_name,
                    self.atlas_poll_timeout_seconds,
                    exc_info=True,
                )
                raise
            # conformance: ignore[E004] the probe is a suite-supplied write and can raise anything; narrowing it would let one connector's exception type escape the retry the loop exists to perform
            except Exception as error:
                # conformance: ignore[E007] the refusal IS the reading — carried out as _SeedProbeReading(error=...) and re-raised by the caller when the wait never settles, so nothing is hidden; a fresh connection 403s child writes until its policies are live, which is the normal case here
                return _SeedProbeReading(error=error)
            return _SeedProbeReading()

        outcome = await poll_until(
            _attempt,
            settled=lambda reading: reading.permitted,
            budget=self._seed_probe_budget(),
            label=f"a permitted child write under {qualified_name}",
        )
        if isinstance(outcome, Settled):
            logger.info(
                "e2e seed: probe write under %s succeeded after %d attempt(s) — "
                "connection policies are live",
                qualified_name,
                outcome.attempts,
            )
            return
        last = getattr(outcome, "last", None)
        logger.error(
            "e2e seed: probe write under %s still failing after %d attempt(s) / "
            "%ds — a fresh connection 403s child writes until its access "
            "policies go live, so this means provisioning never completed",
            qualified_name,
            outcome.attempts,
            self.atlas_poll_timeout_seconds,
        )
        if last is not None and last.error is not None:
            raise last.error
        # No reading at all: the wait could not even run the probe, which
        # ``poll_until`` reports as Indeterminate carrying the cause.
        cause = getattr(outcome, "cause", None)
        raise SeededConnectionNotSearchableError(
            message=(
                f"The seed probe under {qualified_name} never ran to a verdict "
                f"({type(outcome).__name__}), so whether the connection's "
                "policies went live is unknown."
            ),
            resource=qualified_name,
            actual_state="seed probe reached no verdict",
            cause=cause,
        )

    def seed_assets(self, spec: harness_seed.SeedSpec) -> harness_seed.SeededConnection:
        """Seed a lineage parent another *source* owns, through a real publish run.

        For the ``ATLAS-404-00-00A`` class of failure (FND-402 / FND-1648): a
        lineage-only connector (coalesce, adf, mode) publishes Process /
        ColumnProcess entities whose refs name *another source's* assets, and on
        a connector-scoped e2e tenant nothing has ever crawled that source. Call
        this from :meth:`seed_prerequisites` with the exact tree the connector's
        refs will name.

        **Use it only when that source is not reachable inside the leg.** When it
        is — the postgres miner's warehouse is the same app's other entrypoint,
        with a hermetic container already in the job — declare a real crawl in
        :attr:`dag_runs` instead. A crawl seeds from the real producer and its QN
        parity holds by construction; this does not.

        The seed writes transformed NDJSON and submits one ``PublishWorkflow``
        node, so publish owns both the entities *and* the connection cache the
        consuming connector resolves its refs against — see
        :mod:`application_sdk.testing.harness.seed` for why seeding Atlas
        directly cannot produce the second half.

        Unlike :meth:`seed_connection`, which seeds *this run's own* connection,
        the tree here hangs under a **second** ephemeral connection for the
        referenced source. When ``spec.qualified_name`` is ``None`` this run's
        minter names it, exactly as it named the run's own — same uniqueness,
        same predictability. Either way the QN lands on
        ``self._seeded_connection_qns`` and the object-store prefix on
        ``self._seeded_prefixes``, both of which :meth:`teardown_method` cleans
        up after the run's own connection — closing the gap where a suite that
        seeded a second connection had nothing that would ever tear it down.

        Args:
            spec: What to seed. When its ``qualified_name`` is ``None``, one is
                minted from ``spec.connector_type`` (the display name follows
                unless the spec pinned its own). The admin ACL defaults to the
                one ``setup_method`` resolved when the spec declares none.

        Returns:
            The :class:`~application_sdk.testing.harness.seed.SeededConnection`,
            whose ``qualified_name`` is the prefix to rebase the connector's refs
            onto.

        Raises:
            UnknownConnectorTypeError: ``spec.connector_type`` is not a pyatlan
                ``AtlanConnectorType``.
            SeedSegmentInvalidError: A segment cannot compose the qualified name
                the spec declares.
            SeedStoreUnavailableError: No object-store binding the tenant's
                publish app reads — see :meth:`seed_object_store`.
            SeedTreeInvalidError: The serialised batch would not survive publish.
            SeedPublishFailedError: The seed's publish run did not succeed.
        """
        return run_sync(self._seed_assets_async(spec))

    async def _seed_assets_async(
        self, spec: harness_seed.SeedSpec
    ) -> harness_seed.SeededConnection:
        """Resolve the identity, register what teardown must reclaim, then hand off.

        Both registrations happen *before* the seed runs, not after: a seed that
        uploads its NDJSON and then fails in publish has left a prefix and
        (possibly) a connection behind, and registering on success would leave
        exactly those half-set-up artifacts on a shared tenant.
        """
        identity = self._minter.connection_identity(spec.connector_type)
        resolved = spec.resolve(
            qualified_name=spec.qualified_name or identity.qualified_name,
            display_name=spec.display_name or identity.display_name,
        )
        if not (resolved.admin_users or resolved.admin_groups or resolved.admin_roles):
            resolved = resolved.with_admins(
                admin_users=tuple(
                    self.connection_admin_users or self._auto_admin_users
                ),
                admin_groups=tuple(self.connection_admin_groups),
                admin_roles=tuple(
                    self.connection_admin_roles or self._auto_admin_roles
                ),
            )

        plan = self._seed_publish_plan(resolved)
        self._seeded_connection_qns.append(resolved.qualified_name)
        self._seeded_prefixes.append(
            harness_seed.seed_prefix_root(
                app_name=plan.app_name, qualified_name=resolved.qualified_name
            )
        )
        return await harness_seed.seed_assets(
            resolved,
            store=self.seed_object_store(),
            ae=self._ae,
            plan=plan,
            verify=self._count_seeded_assets,
        )

    async def _count_seeded_assets(self, qualified_name: str) -> Outcome[int]:
        """Poll Atlas for the asset count under a seeded connection.

        The read-back that turns "the publish node succeeded" into "the seed
        landed". Polled rather than read once, on the same short budget the run's
        own inventory uses: Elasticsearch is eventually consistent, but assets
        that will appear do so within seconds of publish completing, so a single
        read straight after the verdict would report zero for a seed that is
        merely still indexing.

        Args:
            qualified_name: The seeded connection's QN.

        Returns:
            :class:`~application_sdk.testing.harness.outcome.Settled` carrying
            the count, or the outcome that stopped the poll — which the caller
            grades as *unverified*, never as zero.
        """
        label = f"total asset count under {qualified_name}"
        # The sentinel only survives if ``poll_until`` never ran the probe at
        # all, which its own budget forbids — but an Indeterminate is the honest
        # value for "no reading was taken", and it is what the caller grades as
        # unverified rather than as zero.
        last: Outcome[int] = Indeterminate(
            label=label,
            attempts=0,
            elapsed=timedelta(0),
            cause=RuntimeError("the seeded-asset count was never read"),
        )

        async def _probe() -> Outcome[int]:
            nonlocal last
            async with self._atlas_client() as client:
                last = await atlas.count_total_assets(client, qualified_name)
            return last

        await poll_until(
            _probe,
            settled=lambda reading: isinstance(reading, Settled) and reading.value > 0,
            budget=self._atlas_counts_budget(),
            label=f"seeded assets under {qualified_name}",
        )
        return last

    def _seed_publish_plan(
        self, spec: harness_seed.ResolvedSeedSpec
    ) -> harness_seed.SeedPublishPlan:
        """Resolve how this leg dispatches and waits on a seed's publish run.

        Every value is the one this suite's own run uses, so a seed cannot be
        polled on a different budget or dispatched to a different tenant than the
        run it exists to unblock. The AE workflow name is the exception and is
        deliberately suffixed: ``create_workflow`` is idempotent on the name, so
        sharing one with the suite's own run would publish the seed's graph over
        the connector's.

        Args:
            spec: The resolved spec, whose connection QN names the workflow.

        Returns:
            The plan.
        """
        return harness_seed.SeedPublishPlan(
            app_name=self.connector_short_name,
            publish_task_queue=self._publish_task_queue(),
            ae_workflow_name=(
                f"{self.connector_short_name}-{self.connection_name_prefix}-"
                f"{self.run_id}-seed-{spec.connector_type}"
            ),
            app_service_url=self.app_service_url,
            run_id=self.run_id,
            submit_retry=self._submit_retry(),
            poll_interval_seconds=self.ae_poll_interval_seconds,
            poll_timeout_seconds=self.ae_poll_timeout_seconds,
            progress_stall_seconds=self._resolved_progress_stall_seconds(),
            minter=self._minter,
        )

    def _publish_task_queue(self) -> str:
        """Task queue the tenant's ``publish`` app polls.

        Derived from :meth:`resolved_tenant_deployment_name` rather than pinned,
        for the reason that method exists: which deployment the system apps are
        registered under is a property of the tenant, and one suite runs against
        several tenants in one CI run.
        """
        return f"atlan-publish-{self.resolved_tenant_deployment_name()}"

    def seed_object_store(self) -> ObjectStore:
        """The object store a seed writes its transformed NDJSON into.

        Must be a store the **tenant's** ``publish`` app reads, not the
        connector's deployment store: publish is handed a prefix, and a prefix in
        a bucket it cannot see fails as an empty batch rather than as a missing
        file. In CI that store is the configurator-emitted ``atlan-objectstore``
        Dapr component — the tenant blobstorage binding the sdr-e2e action
        selects into ``ci-deploy/components`` and mounts into the worker — so the
        default resolves exactly that, from the runner's own copy.

        Override on a suite whose leg names the binding differently or has to
        supply secrets explicitly; :envvar:`E2E_SEED_COMPONENTS_DIR` and
        :envvar:`E2E_SEED_STORE_BINDING` cover the common cases without a code
        change.

        Returns:
            An obstore-compatible store.

        Raises:
            SeedStoreUnavailableError: The named component is absent or
                unusable, so there is nowhere to point publish at.
        """
        components_dir = (
            os.environ.get("E2E_SEED_COMPONENTS_DIR", "").strip()
            or _DEFAULT_SEED_COMPONENTS_DIR
        )
        binding = (
            os.environ.get("E2E_SEED_STORE_BINDING", "").strip()
            or _DEFAULT_SEED_STORE_BINDING
        )
        store = create_store_from_binding_optional(
            binding, components_dir=components_dir
        )
        if store is None:
            raise harness_seed.SeedStoreUnavailableError(
                message=(
                    f"no usable Dapr component {binding!r} under {components_dir!r}, "
                    "so a lineage-parent seed has nowhere to write the transformed "
                    "NDJSON the tenant's publish app reads. On the CI path this is "
                    "the configurator-emitted tenant blobstorage binding; point "
                    "E2E_SEED_COMPONENTS_DIR / E2E_SEED_STORE_BINDING at it, or "
                    "override seed_object_store() on this suite"
                ),
                resource=f"{components_dir}/{binding}",
                actual_state="component absent or unusable",
            )
        return store

    # ------------------------------------------------------------------
    # Subclass hooks — override these
    # ------------------------------------------------------------------

    def agent_spec(self) -> AgentSpec | None:
        """Agent identity (tier 4 only). Return None for direct mode.

        Default (AGENT mode): derive the agent identity from the worker's own
        deployment env so the extract node is dispatched to exactly the queue
        the deployed worker polls — no per-connector hard-coding. The worker
        derives its Temporal queue as
        ``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`` (see
        :func:`application_sdk.main._derive_task_queue`); mirroring that here as
        ``agent_name = {ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`` makes
        :meth:`_extract_task_queue` (``atlan-{agent_name}``) equal the worker
        queue automatically. In particular this picks up any *per-leg*
        ``ATLAN_DEPLOYMENT_NAME`` the CI action sets to give each parallel
        matrix leg its own worker + queue (avoiding cross-worker artifact
        invisibility under the two-store posture). Subclasses may still override
        to pin an explicit agent identity.

        Only the two-var shape is derivable from env. When the deployment env is
        absent — e.g. a developer running ``pytest`` locally without the CI
        action's ``ATLAN_DEPLOYMENT_NAME`` — this falls back to
        ``{connector_short_name}-{connection_name_prefix}-{run_id}`` (the same
        run-id-keyed shape connectors used to hard-code in an override). That
        keeps local runs working with no override while CI (which always exports
        the per-leg ``ATLAN_DEPLOYMENT_NAME``) still gets the exact worker queue.
        For a local full-DAG run you must start the connector worker on this same
        run-id queue explicitly — it is **not** what ``main._derive_task_queue``
        builds from the same partial env, so the worker won't land there on its
        own. A one-line ``logger.warning`` is emitted on the fallback path so a
        CI leg that reaches it (env genuinely mis-set) gets an actionable log
        line rather than a silent stall.

        Subclasses should **not** override this to pin a hard-coded run-id name:
        doing so drops the per-leg suffix the worker inherits and desyncs the two
        queues (conformance rule T017). Override only to pin a genuinely
        different agent identity (and then read the deployment env yourself).
        """
        if self.mode is RunMode.DIRECT:
            return None
        app_name = application_name_from_env()
        deployment_name = deployment_name_from_env()
        # Strip the prefix the canonical deriver adds rather than re-assembling
        # the pair here: _extract_task_queue puts "atlan-" back on, and going
        # through derive_task_queue keeps this mirror pinned to the worker's rule
        # instead of being a second implementation of it (FND-195).
        worker_queue = derive_task_queue(app_name, deployment_name)
        if worker_queue is not None and worker_queue.startswith(QUEUE_PREFIX):
            return AgentSpec(agent_name=worker_queue.removeprefix(QUEUE_PREFIX))
        # Local fallback: no CI-exported deployment env. Reproduce the exact
        # {connector}-{connection_name_prefix}-{run_id} shape connectors used to
        # hard-code in a working local override, so a local run lands on its own
        # queue without needing a per-connector override. NB this does NOT match
        # what a worker's main._derive_task_queue() would build from the same
        # partial env — that returns atlan-{app}-{deployment} (both vars),
        # bare {app} (only app), or {ClassName}-queue (neither), never a run-id
        # queue. So for a local full-DAG run the worker must be started on this
        # same agent_name queue explicitly; CI is unaffected (it always exports
        # both vars, taking the exact-match branch above).
        agent_name = (
            f"{self.connector_short_name}-{self.connection_name_prefix}-{self.run_id}"
        )
        # The real Temporal queue is atlan-{agent_name} (_extract_task_queue
        # prepends "atlan-"). Render that one consistent, fully-qualified queue
        # everywhere in the message so a reader doesn't see the name two ways.
        queue = f"atlan-{agent_name}"
        # Fire loud: in CI both vars are always exported, so reaching this branch
        # there means the env is mis-set and the worker will poll a different
        # queue → silent stall until the run-full-dag stall guard trips. Naming
        # both vars makes a mis-set CI leg immediately actionable. (On a genuine
        # local run this is just an FYI that you must start the worker on this
        # queue.)
        logger.warning(
            "AGENT-mode agent_spec fell back to extract queue %s because "
            "ATLAN_APPLICATION_NAME and/or ATLAN_DEPLOYMENT_NAME is unset "
            "(app=%r deployment=%r). In CI both are always exported, so a mis-set "
            "leg here will stall — the worker polls atlan-{app}-{deployment}, not "
            "%s. Locally, start the connector worker on %s.",
            queue,
            app_name,
            deployment_name,
            queue,
            queue,
        )
        return AgentSpec(agent_name=agent_name)

    def connection_spec(self) -> ConnectionSpec:
        """Where the resulting Atlas Connection will live."""
        # Include the {{credentialGuid}} placeholder only when a credential
        # body will be created — public-source connectors (credential_body=None)
        # must NOT send the literal unsubstituted string to Atlas.
        cred_guid = "{{credentialGuid}}" if self._credential_body() is not None else ""
        # Fall back to auto-resolved values when no explicit admins are set.
        # _auto_admin_roles: the $admin role GUID (any tenant admin can manage)
        # _auto_admin_users: the current API token's username (so teardown can purge)
        admin_roles = self.connection_admin_roles or getattr(
            self, "_auto_admin_roles", ()
        )
        admin_users = self.connection_admin_users or getattr(
            self, "_auto_admin_users", ()
        )
        return ConnectionSpec(
            name=self.connection_display_name,
            qualified_name=self.connection_qualified_name,
            connector_name=self.connection_type or self.connector_short_name,
            source_logo=f"https://assets.atlan.com/assets/{self.connector_short_name}.png",
            admin_users=admin_users,
            admin_groups=self.connection_admin_groups,
            admin_roles=admin_roles,
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
        return self.substitutions_class.model_validate(
            {
                "connection": ConnectionRef.model_validate(
                    {"typeName": "Connection", "attributes": spec.attributes()}
                )
            }
        )

    def _credential_body(self) -> CredentialBody | None:
        """Typed credential body posted as ``payload[].body`` to AE.

        Default: None — connector needs no credential body (public
        source). SQL connectors and others that require credentials
        must override to return their codegen'd ``<Connector>CredentialBody``
        instance.
        """
        return None

    def agent_json(self) -> dict[str, Any] | None:
        """Agent-mode routing block forwarded to :func:`build_ae_payload`.

        Default None — no flat ``agent-json.*`` / ``credential-guid.*`` rows are
        emitted (single-entrypoint / direct-mode submits are unaffected).
        Override to return the single-bundle agent_json a connector's auth needs
        (``key-type=""`` + ``secret-manager`` + ``secret-path`` + nested
        ``extra`` ref-keys, or the dotted shape from
        :func:`~application_sdk.testing.e2e.payload.build_agent_json`); the
        harness then emits the routing rows so subclasses no longer override
        ``_build_ae_payload`` just to append them.
        """
        return None

    def resolved_tenant_deployment_name(self) -> str:
        """Deployment name to substitute for ``{deployment_name}``.

        ``E2E_TENANT_DEPLOYMENT_NAME`` wins over the :attr:`tenant_deployment_name`
        class default when set. The class attribute is a property of the *suite*,
        but the value it needs is a property of the *tenant* — and since FND-6 one
        suite runs against several tenants in one CI run, so it cannot be fixed in
        the class. A tenant whose system apps are not registered under
        "production" is then a per-leg env var (supplied by the tenant-matrix
        secret's optional ``deployment_name`` field) rather than an edit here.

        Blank is treated as unset: an unset GitHub Actions env var arrives as an
        empty string, and an empty deployment name would address ``atlan-publish-``
        and fail far from its cause.
        """
        return (
            os.environ.get("E2E_TENANT_DEPLOYMENT_NAME", "").strip()
            or self.tenant_deployment_name
        )

    # ------------------------------------------------------------------
    # Per-run DAG identity and expectations
    # ------------------------------------------------------------------
    #
    # Everything the harness reads about *which* DAG this run submits and *what*
    # it is graded against goes through :attr:`_dag`, never through the
    # ClassVars directly. With no spec in play that resolves to the ClassVars
    # and nothing changes; inside :meth:`run_full_dag` with a spec, it resolves
    # to that spec. That indirection is the whole of what lets one suite run N
    # entrypoint DAGs: the alternative — reading the ClassVars at each call site
    # — makes "the class" and "this run" the same thing by construction.

    @staticmethod
    def _derive_entrypoint(manifest_path: str) -> str:
        """The entrypoint a bundle manifest path names, or ``""``.

        Args:
            manifest_path: ``.../generated/<ep>/manifest.json`` for a bundle
                entrypoint, ``.../generated/manifest.json`` for a single-
                entrypoint app.

        Returns:
            ``<ep>`` when the path carries an entrypoint subdir, else ``""`` —
            which the submit reads as "single-entrypoint app, send no selector".
        """
        marker = "/generated/"
        mp = manifest_path or ""
        if marker in mp and mp.endswith("/manifest.json"):
            tail = mp.split(marker, 1)[1]  # "<ep>/manifest.json" or "manifest.json"
            parts = tail.split("/")
            if len(parts) == 2:  # a subdir <ep> is present
                return parts[0]
        return ""

    def resolve_dag(self, spec: DAGSpec | None) -> ResolvedDAG:
        """Settle *spec* against this suite's class attributes.

        Args:
            spec: The declaration, or ``None`` for "the class's own run". Each
                ``None`` field inherits the class attribute of the same name, so
                ``None`` and ``DAGSpec()`` resolve identically — which is what
                makes an explicitly-declared default run indistinguishable from
                the implicit one, in the AE workflow name included.

        Returns:
            The values this run is submitted and graded with.
        """
        spec = spec or DAGSpec()
        manifest_path = (
            self.manifest_path if spec.manifest_path is None else spec.manifest_path
        )
        entrypoint = self.entrypoint if spec.entrypoint is None else spec.entrypoint
        entrypoint = entrypoint or self._derive_entrypoint(manifest_path)
        return ResolvedDAG(
            label=spec.label or entrypoint or "default",
            entrypoint=entrypoint,
            manifest_path=manifest_path,
            expect_connection=(
                self.expect_connection
                if spec.expect_connection is None
                else spec.expect_connection
            ),
            expect_lineage=(
                self.expect_lineage
                if spec.expect_lineage is None
                else spec.expect_lineage
            ),
            require_nonempty_assets=(
                self.require_nonempty_assets
                if spec.require_nonempty_assets is None
                else spec.require_nonempty_assets
            ),
            required_dag_nodes=(
                tuple(self.required_dag_nodes)
                if spec.required_dag_nodes is None
                else tuple(spec.required_dag_nodes)
            ),
            expected_min_asset_counts=dict(
                self.expected_min_asset_counts
                if spec.expected_min_asset_counts is None
                else spec.expected_min_asset_counts
            ),
            expected_exact_counts=dict(
                self.expected_exact_counts
                if spec.expected_exact_counts is None
                else spec.expected_exact_counts
            ),
            expected_asset_qn_depth=dict(
                self.expected_asset_qn_depth
                if spec.expected_asset_qn_depth is None
                else spec.expected_asset_qn_depth
            ),
            # ``getattr`` rather than the attribute: ``_validate_dag_runs``
            # resolves every declared run inside ``setup_method``, *before* the
            # minter has named this run's connection. Both sides of the
            # comparisons it makes see the same empty string, so the check is
            # unaffected — and by the time a run is submitted the attribute is
            # set.
            connection_qualified_name=(
                getattr(self, "connection_qualified_name", "")
                if spec.connection_qualified_name is None
                else spec.connection_qualified_name
            ),
        )

    @property
    def _dag(self) -> ResolvedDAG:
        """The DAG this run is submitting and grading — the class's by default.

        ``getattr`` rather than an attribute read: a good deal of this class is
        unit-tested on an instance that never ran ``setup_method``, and the
        default has to be the class's own run there too.
        """
        active = getattr(self, "_active_dag", None)
        return active if active is not None else self.resolve_dag(None)

    @contextmanager
    def _dag_run(self, spec: DAGSpec | None) -> Iterator[ResolvedDAG]:
        """Make *spec* the active run for the duration of the block.

        ``None`` is a no-op that yields whatever is already active, so
        ``run_full_dag()`` called with no spec from inside an outer block runs
        that block's DAG rather than resetting to the class's.

        A run that names its own ``connection_qualified_name`` also *rebinds*
        ``self.connection_qualified_name`` for the block, and restores it after.
        Rebinding the attribute rather than threading the resolved value through
        every reader is deliberate: the connection is read at two dozen sites
        (the submit payload, every Atlas probe, the outcome, the evidence
        bundle), they all mean "the connection this run is about", and a
        parameter added to each is a parameter the next site can forget to
        forward. The QN is registered for teardown on the way in, so a run that
        prepares a different connection cannot leave one behind.

        Args:
            spec: The run to activate, or ``None`` to leave the active one
                alone.

        Yields:
            The resolved run in force inside the block.
        """
        if spec is None:
            yield self._dag
            return
        previous = getattr(self, "_active_dag", None)
        previous_qn = getattr(self, "connection_qualified_name", "")
        self._active_dag = self.resolve_dag(spec)
        run_qn = self._active_dag.connection_qualified_name
        if run_qn and run_qn != previous_qn:
            self.connection_qualified_name = run_qn
            if run_qn not in self._seeded_connection_qns:
                self._seeded_connection_qns.append(run_qn)
        try:
            yield self._active_dag
        finally:
            self._active_dag = previous
            self.connection_qualified_name = previous_qn

    def _resolved_entrypoint(self) -> str:
        """App-entrypoint for AE's manifest fetch: explicit ``entrypoint`` if set,
        else derived from ``manifest_path`` (``.../generated/<ep>/manifest.json`` ->
        ``<ep>``). Empty means single-entrypoint (no selector sent).

        Both halves are per-run since FND-1157: a suite running several
        entrypoint DAGs resolves this against the run in flight, not against the
        class.
        """
        return self._dag.entrypoint

    def _database_spec_connector_config_name(self) -> str:
        """The legacy ``DatabaseSpec.connector_config_name``, or ``""``.

        Duck-typed on purpose. ``database_spec()`` is a
        :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest` hook, but
        non-SQL suites define it ad hoc on a plain :class:`BaseE2ETest`
        subclass — and those are exactly the suites that set the deprecated
        field, so a ``SQLAppE2ETest``-only lookup would miss them.

        Returns ``""`` for a suite with no hook at all and for a
        ``SQLAppE2ETest`` subclass that has not overridden the default (which
        raises :class:`HarnessMethodNotImplementedError`); any other exception
        from the hook propagates, because the caller decides whether this
        lookup was load-bearing.
        """
        hook = getattr(self, "database_spec", None)
        if not callable(hook):
            return ""
        try:
            spec = hook()
        except HarnessMethodNotImplementedError:
            return ""
        return getattr(spec, "connector_config_name", "") or ""

    def _warn_legacy_connector_config_name(self, disposition: str) -> None:
        """Emit the ``DatabaseSpec.connector_config_name`` deprecation notice.

        Paired ``DeprecationWarning`` + ``warning`` log line, the same shape
        every other 4.0 deprecation in this SDK uses: the warning so ``-W
        error`` and ``filterwarnings`` can make it fatal ahead of the removal,
        the log line so it is visible in a CI job's captured output where
        nobody is reading Python's warning filter.
        """
        message = (
            f"{type(self).__name__}: DatabaseSpec.connector_config_name is "
            f"deprecated and is removed in "
            f"v{DATABASE_SPEC_CREDENTIAL_TYPE_REMOVAL_VERSION}. {disposition} "
            f"Declare the credential-config name once, on the "
            f"`connector_config_name` ClassVar of the test class (a bundle "
            f"gets it generated into app/generated/<entrypoint>/_e2e_base.py), "
            f"and drop it from database_spec()."
        )
        warnings.warn(message, DeprecationWarning, stacklevel=3)
        logger.warning("%s", message)

    def resolved_connector_config_name(self) -> str:
        """The connector's credential-config name — the one resolution point.

        Precedence:

        1. The :attr:`connector_config_name` ClassVar. The supported place to
           declare it, and what the contract toolkit generates into a bundle's
           ``_e2e_base.py``.
        2. ``DatabaseSpec.connector_config_name``, honoured only when the
           ClassVar is empty.

           .. deprecated:: 3.30.0
              Removed in v4.0 — set the ClassVar instead.
        3. ``""``, leaving :func:`~application_sdk.testing.e2e.payload.build_ae_payload`
           to derive ``f"atlan-connectors-{connector_short_name}"``.

        The deprecated field never outranks the ClassVar: it is hand-written
        per suite while the ClassVar can be generated, so letting the
        hand-written copy win would silently override a generated identity —
        the same trap this precedence exists to close, only mirrored.

        Setting the field warns either way, naming what actually happened to
        the value: honoured (the ClassVar was empty), ignored (the ClassVar
        disagreed), or redundant (both agreed). A suite that declares it is
        told which, instead of having to guess whether it is wired.
        """
        declared = self.connector_config_name
        if declared:
            # The ClassVar already answered; this lookup only decides which
            # deprecation notice to emit, so a hook that cannot be built must
            # not take the payload down with it. A suite that genuinely needs
            # database_spec() calls it again in _credential_body() /
            # _mustache_substitutions(), where the failure carries its own cause.
            try:
                legacy = self._database_spec_connector_config_name()
            except Exception:
                logger.warning(
                    "%s: could not read database_spec() while checking for a "
                    "deprecated connector_config_name; using the "
                    "connector_config_name ClassVar (%s).",
                    type(self).__name__,
                    declared,
                    exc_info=True,
                )
                return declared
            if legacy and legacy != declared:
                self._warn_legacy_connector_config_name(
                    f"It is set to {legacy!r} and was IGNORED — the "
                    f"`connector_config_name` ClassVar ({declared!r}) is "
                    f"authoritative and is what this run submits."
                )
            elif legacy:
                self._warn_legacy_connector_config_name(
                    f"It restates the `connector_config_name` ClassVar "
                    f"({declared!r}) and has no effect."
                )
            return declared

        legacy = self._database_spec_connector_config_name()
        if legacy:
            self._warn_legacy_connector_config_name(
                f"It is set to {legacy!r} and the `connector_config_name` "
                f"ClassVar is empty, so this run is submitting the field's "
                f"value."
            )
        return legacy

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
            entrypoint=self._resolved_entrypoint(),
            agent_json=self.agent_json(),
            credential_type=self.resolved_connector_config_name(),
        )

    def _build_legacy_seed_dag(self, extract_queue: str) -> dict[str, Any]:
        """Build a hand-crafted seed DAG when ``manifest_path`` is unset.

        Base class always raises — only :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`
        overrides this (it has the SQL-specific task-queue and DAG-shape
        knowledge needed). Non-SQL connectors must always ship a
        manifest.json and set ``manifest_path``.
        """
        raise HarnessMethodNotImplementedError(
            message="non-SQL connectors must set manifest_path; SQL connectors must override _build_legacy_seed_dag()",
            operation="_build_legacy_seed_dag",
        )

    # ------------------------------------------------------------------
    # Seed DAG — loaded from the connector's manifest.json
    # ------------------------------------------------------------------

    def _seed_dag_from_manifest(self, extract_task_queue: str) -> dict:
        """Load the connector's manifest.json and use it as the seed DAG."""

        path = Path(self._dag.manifest_path)
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
        manifest = orjson.loads(path.read_bytes())
        dag = manifest.get("dag")
        if not isinstance(dag, dict) or not dag:
            raise ManifestDagMissingError(
                message=f"Manifest at {path} has no top-level `dag` object — can't use as a seed DAG.",
                location=str(path),
            )

        deployment_name = self.resolved_tenant_deployment_name()

        def _sub_queue(node_name: str, raw: str) -> str:
            if node_name == "extract":
                return extract_task_queue
            return raw.replace("{deployment_name}", deployment_name)

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
        """Recursively replace exact-match ``{{...}}`` strings.

        Delegates to the shared walker (``application_sdk.testing._mustache``) so
        the e2e and SDR harnesses share one implementation and cannot drift.
        """
        from application_sdk.testing._mustache import (  # noqa: PLC0415
            apply_mustache_subs,
        )

        return apply_mustache_subs(obj, subs)

    # ------------------------------------------------------------------
    # The actual flow
    # ------------------------------------------------------------------

    def _extract_task_queue(self) -> str:
        """Task queue the ``extract`` node is dispatched to.

        Single source of truth for both the seed DAG (which pins the extract
        node's ``task_queue`` to this value) and the stall-guard diagnostic. In
        AGENT mode this must equal the queue the deployed worker polls
        (``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``), so the
        test's ``agent_spec().agent_name`` has to match that suffix.
        """
        agent = self.agent_spec()
        if agent is not None:
            return f"atlan-{agent.agent_name}"
        return f"atlan-{self.connector_short_name}-default"

    def _resolved_progress_stall_seconds(self) -> int:
        """Progress-watchdog window this run will actually use.

        Returns ``dag_progress_stall_seconds`` when the suite pinned one
        (including 0, which disables the watchdog), else a window derived from
        ``ae_poll_timeout_seconds``. ``setup_method`` has already rejected a
        pinned value the poll loop could never reach.
        """
        pinned = type(self).dag_progress_stall_seconds
        if pinned is not None:
            return pinned
        return _derive_progress_stall_seconds(self.ae_poll_timeout_seconds)

    def _capture_node_dispatch(self, seed_dag: dict[str, Any]) -> None:
        """Record node name -> (app_name, task_queue) from the seed DAG.

        Read back only by :meth:`_describe_dag_nodes`, to name the queue a
        never-dispatched node was waiting on. Reads the *resolved*
        ``inputs.task_queue`` in preference to the node-level
        ``app_task_queue``: only the former has its ``{deployment_name}``
        placeholder substituted by :meth:`_seed_dag_from_manifest`.
        """
        dispatch: dict[str, NodeDispatch] = {}
        for name, node in seed_dag.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            queue = inputs.get("task_queue") if isinstance(inputs, dict) else None
            if not isinstance(queue, str) or not queue:
                queue = node.get("app_task_queue")
            app_name = node.get("app_name")
            dispatch[name] = NodeDispatch(
                app_name=app_name if isinstance(app_name, str) else "",
                task_queue=queue if isinstance(queue, str) else "",
            )
        self._node_dispatch = dispatch

    def _capture_expected_node_identities(self, seed_dag: dict[str, Any]) -> None:
        """Record the node identities the app under test declares.

        Read back only by :meth:`_assert_deployed_manifest_matches`.

        Captured from the seed DAG rather than by re-reading the file: the seed
        DAG *is* the manifest's ``dag``, already carrying this harness's
        ``{app_name}`` substitution, so the two sides of the later comparison
        are normalised the same way. A connector with no ``manifest_path`` built
        its seed DAG by hand (``_build_legacy_seed_dag``) — that is an
        approximation of the app's graph, not a copy of it, so there is nothing
        to compare and the identities stay empty.
        """
        if not self._dag.manifest_path:
            logger.info(
                "manifest_path empty — the deployed-manifest identity check has "
                "no committed DAG to compare against and will self-skip"
            )
            self._expected_node_identities = {}
            return
        self._expected_node_identities = node_identities(
            seed_dag, app_name=self.connector_short_name
        )

    def _build_seed_dag(self) -> dict[str, Any]:
        """Build this run's seed DAG and record what it says about each node.

        Returns:
            The graph to publish as the workflow's seed version — the manifest's
            own ``dag`` when the suite ships one, else the hand-crafted fallback.

        Raises:
            HarnessMethodNotImplementedError: ``manifest_path`` is empty and the
                suite is not a SQL connector (the only one with a legacy
                fallback).
        """
        extract_queue = self._extract_task_queue()
        if self._dag.manifest_path:
            seed_dag = self._seed_dag_from_manifest(extract_queue)
        else:
            logger.info("manifest_path empty — falling back to _build_legacy_seed_dag")
            seed_dag = self._build_legacy_seed_dag(extract_queue)
        self._capture_node_dispatch(seed_dag)
        self._capture_expected_node_identities(seed_dag)
        return seed_dag

    def _ae_workflow_name_suffix(self) -> str:
        """``-<label>`` for a run that is not this suite's own, else ``""``.

        ``create_workflow`` reuses an existing workflow of the same name, so
        without this every run in a multi-DAG suite would publish its seed over
        the previous run's — one AE workflow carrying two different graphs, and
        an AE run list in which a crawl and a mine are indistinguishable.

        The suffix is keyed off the *resolved* run rather than off "was a spec
        passed", so a suite that spells its own run out as an explicit
        ``DAGSpec()`` gets the same workflow name it would have got implicitly.
        That is what keeps every pre-existing suite's AE workflow name byte-identical.

        Returns:
            The suffix, or ``""`` when this run resolves to the class's own.
        """
        dag = self._dag
        if dag == self.resolve_dag(None):
            return ""
        return f"-{dag.label}"

    def _ae_workflow_spec(self) -> AEWorkflowSpec:
        """Describe the AE workflow this run seeds and submits against.

        The connector policy, as one value the shared starter takes: what to
        call the workflow, what graph to seed it with, and — for a suite pointed
        at a workflow someone else maintains — which existing slug to submit
        against instead, in which case nothing is seeded over it.

        Returns:
            The spec. :attr:`~application_sdk.testing.harness.starters.AEWorkflowSpec.payload`
            is deliberately left empty: the submit body carries the slug
            (``metadata.ae_workflow_slug``), and AE mints that slug on the
            create, so the payload cannot exist until the publish has answered.
            That is why this run publishes through
            :func:`~application_sdk.testing.harness.starters.publish_seed_version`
            and submits separately, rather than through the all-in-one
            ``start_via_automation_engine``.
        """
        return AEWorkflowSpec(
            name=(
                self.ae_workflow_name_override
                or f"{self.connector_short_name}-{self.connection_name_prefix}-{self.run_id}"
            )
            + self._ae_workflow_name_suffix(),
            description=f"Full-DAG e2e harness — {self.connector_short_name}",
            slug=self.ae_workflow_slug,
            seed_dag=self._build_seed_dag() if not self.ae_workflow_slug else {},
        )

    async def _bootstrap_workflow(self) -> str:
        """Ensure an AE workflow exists with a published version.

        Three of the four AE writes, and none of them lives here any more: the
        create, the slug-index read and the create-then-publish pair are
        :func:`~application_sdk.testing.harness.starters.publish_seed_version`.
        What this method still owns is the *policy* — the workflow's name, the
        seed graph, and whether to reuse a slug the suite pinned.

        Returns:
            The slug to submit against. The seed version this published, if any,
            lands on ``self._seed_version``, where the deployed-manifest check
            reads it.
        """
        seeded = await publish_seed_version(
            self._ae_workflow_spec(), client=self._ae, minter=self._minter
        )
        self._seed_version = seeded.seed_version
        return seeded.slug

    # ------------------------------------------------------------------
    # Failure diagnostics
    # ------------------------------------------------------------------

    def _dag_outcome_headline(self, ae_result: DAGRunResult) -> str:
        """One line saying what the DAG did, before the per-node breakdown.

        A poll that stopped early is NOT a verdict on the nodes: it says the
        harness stopped watching. Saying so explicitly is the difference between
        "the node failed" (look for a bug in the node's code) and "the node was
        never dispatched" (look for the worker that should have picked it up).
        Both early exits get that treatment — the poll ceiling and the progress
        watchdog, which is the one that fires first on any suite whose ceiling
        is at or below 1800s.
        """
        if not ae_result.stopped_watching:
            return f"AE status={ae_result.status.value}"
        stalled = ae_result.seconds_since_last_progress
        stall_note = (
            f"; no DAG node changed state for the last {int(stalled)}s"
            if stalled is not None
            else ""
        )
        if ae_result.progress_stalled:
            return (
                f"DAG stopped making progress: the "
                f"{int(ae_result.progress_stalled_after_seconds or 0)}s watchdog "
                f"window closed before the poll ceiling "
                f"(AE status={ae_result.status.value}){stall_note}"
            )
        return (
            f"DAG did not complete within "
            f"{int(ae_result.timed_out_after_seconds or 0)}s "
            f"(AE status={ae_result.status.value}){stall_note}"
        )

    def _dispatch_note(self, node_name: str) -> str:
        """``on task queue 'X' (app_name=Y, per the seed DAG)`` when known."""
        dispatch = self._node_dispatch.get(node_name)
        if dispatch is None or not dispatch.task_queue:
            return "task queue not resolvable from the seed DAG"
        app_note = f", app_name={dispatch.app_name}" if dispatch.app_name else ""
        return f"task queue '{dispatch.task_queue}'{app_note}, per the seed DAG"

    def _stop_point_clause(self, ae_result: DAGRunResult) -> str:
        """`` at the Xs poll ceiling`` / `` when the Xs watchdog closed``, or ``""``.

        Names which early exit produced this observation, so a node line can say
        where the harness stopped without each branch re-deriving it — and so a
        600s watchdog stop is never reported as the 1800s ceiling.
        """
        if ae_result.progress_stalled:
            return (
                " when the "
                f"{int(ae_result.progress_stalled_after_seconds or 0)}s progress "
                "watchdog closed"
            )
        if ae_result.timed_out:
            return (
                f" at the {int(ae_result.timed_out_after_seconds or 0)}s poll ceiling"
            )
        return ""

    def _describe_dag_node(self, node: DAGNodeResult, ae_result: DAGRunResult) -> str:
        """One breakdown line for a node, in terms of what an operator should do.

        The three cases that used to render identically as
        ``status=<X> error=None``:

        * AE reports it not started (``Pending`` / ``Scheduled``) — which does
          not say whether anything picked it up, so the line names the queue and
          the child workflow to read, and asserts no cause (see
          :attr:`~application_sdk.testing.e2e.client.DAGNodeStatus.is_not_started`);
        * dispatched but never finished (``Running`` where the poll stopped) —
          the worker took it and stopped making progress, so the queue is named
          too;
        * ran and failed — the error message is the whole story.

        Both places the poll can stop early read the same: the ceiling and the
        progress watchdog only differ in the clause naming which one closed.
        """
        stalled = ae_result.seconds_since_last_progress
        stall_clause = (
            f"; no DAG state change for the last {int(stalled)}s"
            if stalled is not None
            else ""
        )
        stop_point = self._stop_point_clause(ae_result)
        if node.status.is_success:
            duration = node.duration_seconds
            timing = f" in {int(duration)}s" if duration is not None else ""
            return f"  - {node.name}: succeeded{timing}"
        if node.status.is_skipped:
            return f"  - {node.name}: {node.status.value} (AE did not run it)"
        if node.status.is_not_started:
            return (
                f"  - {node.name}: AE reports {node.status.value}{stop_point} — "
                f"{self._dispatch_note(node.name)}{stall_clause}. AE holds a node "
                f"at {node.status.value} whether nothing picked it up OR its child "
                "workflow is running, so read the child workflow "
                f"'{ae_result.run_id}-{node.name}' on the tenant's Temporal: no "
                "such execution means nothing polled that queue (check the owning "
                "app's workers); an execution means it is running or retrying "
                "(check its history for heartbeat timeouts)."
            )
        if node.status is DAGNodeStatus.RUNNING and ae_result.stopped_watching:
            return (
                f"  - {node.name}: STILL RUNNING{stop_point} — "
                f"dispatched to {self._dispatch_note(node.name)}{stall_clause}. A "
                "worker took it and stopped making progress (or died holding it)."
            )
        return f"  - {node.name}: status={node.status.value} error={node.error_message}"

    def _describe_dag_nodes(self, ae_result: DAGRunResult) -> str:
        """Per-node breakdown, every node — succeeded ones included.

        The successes carry the timing that shows *where* the DAG got to before
        it stopped, which is what points at the stuck node's upstream.
        """
        if not ae_result.nodes:
            return "  (no DAG nodes reported)"
        return "\n".join(self._describe_dag_node(n, ae_result) for n in ae_result.nodes)

    def _core_dag_ok(self, ae_result: DAGRunResult) -> bool:
        """Skip-tolerant success gate for the DAG run.

        The strict ``all_nodes_succeeded`` requires every DAG node to reach
        ``Succeeded``. That is correct when lineage is expected, but with
        ``expect_lineage`` False the AE legitimately Skips the qi + lineage
        nodes — so the strict gate reports a false failure on a metadata-only
        crawl whose ``extract -> publish`` path fully succeeded and landed
        assets in Atlas.

        Behaviour:
          * ``expect_lineage`` True  -> unchanged strict gate
            (``all_nodes_succeeded``), so existing lineage connectors keep
            passing exactly as before.
          * ``expect_lineage`` False -> every node in ``required_dag_nodes``
            must genuinely succeed AND no node may be in a hard-failure state
            (``Failed`` / ``Error`` / ``Cancelled``, or carrying an error
            message). Intentionally-skipped downstream nodes (``Skipped`` /
            ``Omitted``, or ``Pending`` from an older service that downgrades
            Skipped) are tolerated.

        The Atlas-side floors + non-empty backstop still run afterwards, so a
        crawl that passes this gate but silently landed nothing still fails.
        """
        if self._dag.expect_lineage:
            return ae_result.all_nodes_succeeded
        by_name = {n.name: n for n in ae_result.nodes}
        required_ok = all(
            name in by_name and by_name[name].status.is_success
            for name in self._dag.required_dag_nodes
        )
        no_hard_failure = not any(
            n.status in _HARD_FAIL_NODE_STATUSES or n.error_message
            for n in ae_result.nodes
        )
        return required_ok and no_hard_failure

    def _submit_retry_kwargs(self) -> dict[str, int]:
        """Re-size ``submit_workflow``'s retry to the tenant-app cold-start budget.

        Thin binding of this harness' ``app_ready_*`` class attrs onto
        :func:`~application_sdk.testing.harness.automation_engine.cold_start_submit_kwargs`,
        which carries the rationale and the validation. See
        ``app_ready_timeout_seconds`` for the budget rationale.

        Returns:
            The ``retries`` / ``retry_sleep_seconds`` pair, or an empty mapping
            when the cold-start budget is disabled.

        Raises:
            MissingHarnessClassAttrError: when ``app_ready_timeout_seconds`` is
                positive but ``app_ready_poll_interval_seconds`` is not.
        """
        return cold_start_submit_kwargs(
            self.app_ready_timeout_seconds,
            self.app_ready_poll_interval_seconds,
        )

    def _submit_retry(self) -> SubmitRetry | None:
        """The same sizing as a typed value, for the submit call.

        Returns:
            The retry sizing, or ``None`` to leave ``submit_workflow``'s own
            defaults in place. Derived through
            :meth:`SubmitRetry.for_cold_start
            <application_sdk.testing.harness.starters.SubmitRetry.for_cold_start>`,
            which calls the same arithmetic
            :meth:`_submit_retry_kwargs` returns — one derivation, two
            spellings, and the mapping stays because connector suites assert on
            it.

        Raises:
            MissingHarnessClassAttrError: See :meth:`_submit_retry_kwargs`.
        """
        return SubmitRetry.for_cold_start(
            timeout_seconds=self.app_ready_timeout_seconds,
            poll_interval_seconds=self.app_ready_poll_interval_seconds,
        )

    # ------------------------------------------------------------------
    # Deployed-manifest identity check
    # ------------------------------------------------------------------

    async def _read_superseding_published_version(
        self, slug: str
    ) -> PublishedVersion | None:
        """Wait briefly for AE to serve a published version past the seed.

        Args:
            slug: The AE workflow slug the harness submitted against.

        Returns:
            The published version once it carries a DAG and provably superseded
            the harness's seed, else ``None`` — the honest answer for both an
            unreadable response and a supersede that never showed up inside the
            budget. ``None`` is never a mismatch.
        """
        seed = self._seed_version
        # The last read that came back at all, not the last read: a transient
        # blip after a good read must not make the diagnostic claim AE was never
        # readable, which is a different (and wronger) thing to tell an operator.
        published: PublishedVersion | None = None

        async def _read() -> PublishedVersion | None:
            nonlocal published
            read = await self._ae.get_published_version(slug)
            if read is not None:
                published = read
            return read

        outcome = await poll_until(
            _read,
            settled=lambda read: read is not None
            and bool(read.dag)
            and _supersedes(read.version, seed),
            budget=self._deployed_manifest_budget(),
            label=f"the DAG AE published for slug {slug}",
        )
        if isinstance(outcome, Settled) and outcome.value is not None:
            return outcome.value
        if published is None:
            # get_published_version already logged why each read failed.
            logger.warning(
                "Deployed-manifest identity check skipped for slug %s: AE's "
                "published version was never readable within %ds, so whether "
                "the executed DAG is this repo's DAG stays unverified",
                slug,
                self.deployed_manifest_timeout_seconds,
            )
            return None
        logger.warning(
            "Deployed-manifest identity check skipped for slug %s: after %ds AE "
            "serves version %r (the harness's own seed version is %r) with %d "
            "node(s), which does not prove Heracles' re-fetch of the tenant "
            "pod's manifest superseded the seed — an absent version number "
            "cannot, and an equal one says it did not. Comparing the seed DAG "
            "against itself would report a match regardless of what the tenant "
            "runs, so nothing is asserted",
            slug,
            self.deployed_manifest_timeout_seconds,
            published.version,
            seed,
            len(published.dag),
        )
        return None

    async def _assert_deployed_manifest_matches(self, slug: str) -> None:
        """Assert the DAG AE published is the DAG this repo's manifest declares.

        Runs immediately after submit and before the poll loop. Post-submit is
        the one thing lost versus the preflight this replaces (which AE does not
        offer — see
        :meth:`~application_sdk.testing.harness.automation_engine.AEClient.get_published_version`),
        but it still fails within seconds of submit and long before any
        assertion, with a node-level diff instead of a confusing downstream
        failure.

        Compares node identity, not the DAG blob: template variables are
        substituted at submit, so a byte comparison would fail on every run.

        Args:
            slug: The AE workflow slug the harness submitted against.

        Raises:
            DeployedManifestMismatchError: The read got through, AE's published
                version provably superseded the harness's seed, and the node
                identities still disagree.
        """
        if not self.assert_deployed_manifest:
            logger.info(
                "assert_deployed_manifest is off for %s — not checking whether "
                "the executed DAG is this repo's DAG",
                type(self).__name__,
            )
            return
        expected = self._expected_node_identities
        if not expected:
            logger.info(
                "Deployed-manifest identity check skipped for slug %s: this "
                "suite has no manifest-derived seed DAG to compare against",
                slug,
            )
            return
        published = await self._read_superseding_published_version(slug)
        if published is None:
            return
        actual = node_identities(published.dag, app_name=self.connector_short_name)
        diff = compare_node_identities(expected, actual)
        if diff.matches:
            logger.info(
                "Deployed-manifest identity check passed: AE published version "
                "%r for slug %s runs the %d node(s) this repo's manifest "
                "declares (%s), so the executed DAG is the app under test's",
                published.version,
                slug,
                len(expected),
                ", ".join(sorted(expected)),
            )
            return
        raise DeployedManifestMismatchError(
            message=(
                f"The DAG AE published at submit is not the DAG "
                f"{type(self).__name__} built from {self._dag.manifest_path}.\n"
                f"At submit, Heracles re-fetches the manifest from the "
                f"tenant-deployed pod and publishes it over the harness's seed "
                f"version, so this is the graph that will actually run — and it "
                f"is not this repo's. The tenant is very likely running a "
                f"different build of the app than the one under test.\n"
                f"slug={slug} published_version={published.version!r} "
                f"seed_version={self._seed_version!r}\n"
                f"local nodes:     {', '.join(sorted(expected)) or '(none)'}\n"
                f"published nodes: {', '.join(sorted(actual)) or '(none)'}\n"
                f"differences:\n{diff.render()}"
            ),
            observed=diff.render(),
            location=f"AE workflow slug {slug}",
        )

    def run_full_dag(self, spec: DAGSpec | None = None) -> FullDAGOutcome:
        """Submit, poll AE, poll Atlas, return the combined outcome.

        Args:
            spec: Which entrypoint DAG to run, and what to grade it against.
                ``None`` (the default, and the only value any suite passed
                before FND-1157) runs the DAG this suite's ClassVars name — or,
                inside :meth:`_dag_run`, the run that block activated. A spec
                overrides only the fields it sets; see :class:`DAGSpec`.

        Returns:
            What the run produced. Subclasses build their own assertions on it —
            see :meth:`_assert_full_dag_outcome` for the default ladder. One
            outcome per call, never a composite: a crawl and a mine assert
            different things.
        """
        with self._dag_run(spec):
            return run_sync(self._run_full_dag_async())

    async def _run_full_dag_async(self) -> FullDAGOutcome:
        """Seed, submit, poll, then read Atlas.

        Returns:
            The combined outcome.

        Raises:
            DAGProgressStalledError: The progress watchdog closed on a wedged
                node. Re-raised with the per-node breakdown attached.
            NoWorkerOnTaskQueueError: Nothing picked the extract node up inside
                the stall grace. When a Temporal address is configured, the
                message carries who *is* polling the queue rather than only the
                inference.
        """
        slug = await self._bootstrap_workflow()
        payload = self._build_ae_payload(slug)

        # Override-proof app-entrypoint injection. Per-app subclasses commonly
        # override _build_ae_payload (to add flat credential-guid.*/agent-json.*
        # rows) and call build_ae_payload() directly WITHOUT forwarding
        # ``entrypoint=`` — so setting metadata.entrypoint only inside
        # build_ae_payload silently misses them. Set it here, after the payload is
        # built, where it always runs. Multi-entrypoint connectors (crawler/miner,
        # extract/lineage) 404 "No manifest available" at AE submit without it.
        resolved_entrypoint = self._resolved_entrypoint()
        if resolved_entrypoint:
            payload.setdefault("metadata", {})["entrypoint"] = resolved_entrypoint

        logger.info(
            "Submitting AE workflow: connector=%s dag=%s mode=%s qn=%s",
            self.connector_short_name,
            self._dag.label,
            self.mode.value,
            self.connection_qualified_name,
        )
        run_id = await self._submit(payload, slug=slug)
        logger.info("AE submit returned run_id=%s", run_id)

        # Before the poll: submit is what makes Heracles fetch the tenant pod's
        # manifest and publish it over our seed, so this is the earliest point
        # the executed graph is knowable — and still seconds in, long before any
        # assertion could pass against the wrong app.
        await self._assert_deployed_manifest_matches(slug)

        ae_result = await self._poll_dag(run_id)

        # Log-only, outcome-neutral. Placed after the poll so Elasticsearch
        # indexing lag cannot masquerade as an absence. See
        # probe_run_is_listed — this is what settles FND-676's gate.
        await self._ae.probe_run_is_listed(slug, run_id)

        return await self._read_atlas(ae_result)

    async def _submit(self, payload: dict[str, Any], *, slug: str) -> str:
        """POST the run to AE, with the tenant-app cold-start budget applied.

        Args:
            payload: The submit body.
            slug: The AE slug the run belongs to. Passed on so an ambiguous
                submit timeout can be resolved by reading AE's own run list
                under this slug instead of failing the leg outright.

        Returns:
            AE's run id.
        """
        retry = self._submit_retry()
        if retry is None:
            return await self._ae.submit_workflow(payload, slug=slug)
        return await self._ae.submit_workflow(
            payload,
            slug=slug,
            retries=retry.retries,
            retry_sleep_seconds=retry.sleep_seconds,
        )

    async def _poll_dag(self, run_id: str) -> DAGRunResult:
        """Poll ``native-status`` until the DAG settles, with this suite's guards.

        Args:
            run_id: AE's run id.

        Returns:
            The final (or last-observed) DAG snapshot.

        Raises:
            DAGProgressStalledError: Re-raised carrying the per-node breakdown.
                The watchdog is the exit that fires first on any suite whose
                ceiling is at or below 1800s — i.e. the FND-708 shape reaches
                the operator here, not through the ceiling return — and raising
                it bare skipped the per-node diagnosis entirely.
            NoWorkerOnTaskQueueError: Re-raised carrying the observed pollers,
                when a Temporal address is configured.
        """
        try:
            return await self._ae.poll_native_status(
                run_id,
                interval_seconds=self.ae_poll_interval_seconds,
                timeout_seconds=self.ae_poll_timeout_seconds,
                stall_grace_seconds=self.ae_stall_grace_seconds,
                stall_task_queue=self._extract_task_queue(),
                progress_stall_seconds=self._resolved_progress_stall_seconds(),
            )
        except DAGProgressStalledError as stalled:
            if stalled.result is None:
                raise
            raise DAGProgressStalledError(
                message=(
                    f"Full-DAG e2e stalled for connector={self.connector_short_name}\n"
                    f"AE run_id={stalled.result.run_id} "
                    f"slug={stalled.result.workflow_slug}\n"
                    f"{self._dag_outcome_headline(stalled.result)}\n"
                    f"DAG nodes:\n{self._describe_dag_nodes(stalled.result)}"
                ),
                result=stalled.result,
                # Keeps the client-level raise in ``cause_repr`` on the wire
                # envelope, not only in the traceback.
                cause=stalled,
            ) from stalled
        except NoWorkerOnTaskQueueError as unpolled:
            observed = await self._observed_pollers()
            if observed is None:
                raise
            # The same error, carrying what was observed — not a new one wrapping
            # it. Two reasons, and the second is the one that matters: rebuilding
            # the leaf would put Temporal's answer and the stall grace's
            # *inference* into one message with nothing to tell a reader which
            # half was measured; and a caller matching on this leaf is entitled
            # to the identity it caught. The field is the machine-readable half,
            # the note is what a red CI leg actually prints.
            unpolled.observed_pollers = observed
            unpolled.add_note(f"Temporal was asked directly: {observed}")
            raise

    async def _observed_pollers(self) -> str | None:
        """Read who is actually polling the extract queue, when that is possible.

        The stall guard's verdict is an *inference*: nothing started inside the
        grace, so probably nothing is polling. Temporal can answer the question
        directly — see
        :mod:`application_sdk.testing.harness.temporal` — and an empty poller
        list is the observed form of the same finding, available on the first
        probe rather than after three minutes of silence.

        Opt-in, because the connector CI leg cannot reach a tenant's Temporal
        frontend: the runner has no route into the tenant vcluster, which is the
        same constraint that makes the AE submit the only tenant-facing probe of
        the installed app pod. A suite that *does* have a route (a local cluster,
        an in-cluster driver) sets :attr:`temporal_address` or exports
        ``E2E_TEMPORAL_ADDRESS``, and gets the observation appended to the
        inference rather than replacing it — the inference is still what fired.

        Returns:
            A line naming the pollers Temporal reports, or ``None`` when no
            address is configured or the read itself failed. ``None`` leaves the
            original diagnostic exactly as it was: a Temporal that cannot be
            read must never turn a real finding into a harness error.
        """
        address = self._resolved_temporal_address()
        if not address:
            return None
        queue = self._extract_task_queue()
        namespace = self._resolved_temporal_namespace()
        try:
            from application_sdk.testing.harness.temporal import (  # noqa: PLC0415
                TemporalServiceReader,
                frontend_connection,
            )

            async def _connect() -> Any:
                return await frontend_connection(address=address, namespace=namespace)

            async with TemporalServiceReader(connect=_connect) as reader:
                pollers = await reader.task_queue_pollers(queue, namespace=namespace)
        except Exception:
            logger.warning(
                "could not read Temporal at %s for task queue %s, so the "
                "no-worker diagnosis stays an inference from the stall grace",
                address,
                queue,
                exc_info=True,
            )
            return None
        if not pollers:
            return (
                f"Temporal confirms it: {queue!r} in namespace {namespace!r} has "
                "NO pollers at all. Nothing is holding that queue, so the "
                "agent_spec().agent_name and the deployed worker's queue do not "
                "match (or the worker is not running)."
            )
        identities = ", ".join(
            f"{poller.identity} ({poller.task_queue_type.value}"
            + (f", build {poller.build_id}" if poller.build_id else "")
            + ")"
            for poller in pollers
        )
        return (
            f"Temporal reports {len(pollers)} poller(s) on {queue!r} in namespace "
            f"{namespace!r}: {identities}. Something IS holding that queue, so "
            "the node was not picked up for another reason — read the child "
            "workflow's history rather than hunting a queue-name mismatch."
        )

    def _resolved_temporal_address(self) -> str:
        """Temporal frontend to read pollers from, or ``""`` to stay inferential.

        Returns:
            ``E2E_TEMPORAL_ADDRESS`` when the ambient environment sets it, else
            the :attr:`temporal_address` class attribute. Blank is treated as
            unset: an unset GitHub Actions env var arrives as an empty string.
        """
        return (
            os.environ.get("E2E_TEMPORAL_ADDRESS", "").strip() or self.temporal_address
        )

    def _resolved_temporal_namespace(self) -> str:
        """Temporal namespace the poller read is scoped to.

        Returns:
            ``E2E_TEMPORAL_NAMESPACE`` when set, else the
            :attr:`temporal_namespace` class attribute.
        """
        return (
            os.environ.get("E2E_TEMPORAL_NAMESPACE", "").strip()
            or self.temporal_namespace
        )

    async def _read_atlas(self, ae_result: DAGRunResult) -> FullDAGOutcome:
        """Read what the run landed in Atlas, and assemble the outcome.

        Args:
            ae_result: The DAG snapshot the poll returned.

        Returns:
            The combined outcome. Every Atlas reading is carried in *both*
            shapes: the settled projection connector suites index, and the raw
            reading the assertion ladder grades — so an unreadable search stays
            distinguishable from a zero all the way to the verdict.
        """
        if not self._dag.expect_connection:
            # This entrypoint publishes no connection inventory, so every Atlas
            # probe below would assert against something it never produces. Skip
            # them and let _core_dag_ok be the verdict.
            #
            # connection_in_atlas stays False because nothing was observed — the
            # field means "the harness saw it", never "it is absent". The
            # assertion does not consult it in this mode (see
            # test_full_dag_runs_end_to_end), so a False here cannot fail a run.
            logger.info(
                "expect_connection is False for %s — skipping the Atlas connection, "
                "asset-count and lineage probes; the DAG outcome is the verdict",
                type(self).__name__,
            )
            return self._outcome(ae_result, connection_in_atlas=False)

        if not self._core_dag_ok(ae_result):
            # Deliberately not "failed: <names>": a Pending node at the poll
            # ceiling never ran, and calling that a failure sent the reader
            # looking for a bug in code that was never dispatched.
            logger.warning(
                "Skipping Atlas probe — %d/%d DAG nodes did not succeed. %s\n%s",
                len(ae_result.failed_nodes),
                len(ae_result.nodes),
                self._dag_outcome_headline(ae_result),
                self._describe_dag_nodes(ae_result),
            )
            return self._outcome(ae_result, connection_in_atlas=False)

        async with self._atlas_client() as client:
            found = await atlas.poll_for_connection(
                client,
                self.connection_qualified_name,
                budget=self._atlas_connection_budget(),
            )
            connection_in_atlas = isinstance(found, Settled) and found.value
            if not connection_in_atlas:
                # The verdict goes with it. Flattening to False here is what made
                # an unreadable Atlas report as "the Connection just did not
                # land" — a claim about the publish path, made by a run that
                # never got an answer.
                return self._outcome(
                    ae_result, connection_in_atlas=False, connection_read=found
                )
            return await self._read_inventory(client, ae_result, connection_read=found)

    async def _read_inventory(
        self,
        client: AsyncAtlanClient,
        ae_result: DAGRunResult,
        *,
        connection_read: Outcome[bool] | None = None,
    ) -> FullDAGOutcome:
        """Read counts, the all-types total, lineage and location samples.

        Args:
            client: The already-open Atlas client the connection poll used.
            ae_result: The DAG snapshot, carried onto the outcome.
            connection_read: The Connection poll's verdict, carried through.

        Returns:
            The outcome with every Atlas reading attached.
        """
        # Probe the union of types referenced by floors, exact-count parity,
        # AND location-depth checks, so all three kinds of expectation get real
        # Atlas counts and the post-loop location sample reads populated data
        # once those types have indexed.
        #
        # NOTE: adding a location type here does NOT by itself make the poll
        # WAIT for that type — the loop only stays alive via a per-type floor or
        # the non-empty backstop (total == 0), so a location-only type with no
        # floor can still be zero when the loop exits (the moment any other type
        # makes total > 0). The real wait-for-this-type safeguard is pairing each
        # expected_asset_qn_depth type with an expected_min_asset_counts floor —
        # see that attr's docstring.
        dag = self._dag
        probe_types = tuple(
            {
                *dag.expected_min_asset_counts,
                *dag.expected_exact_counts,
                *dag.expected_asset_qn_depth,
            }
        )
        count_reads: Mapping[str, CountRead] = {}
        if probe_types:
            count_reads = await self._poll_asset_counts(client, probe_types)

        # True total across ALL asset types (not just the probed ones). The
        # non-empty backstop uses this so it also protects connectors that
        # declare no per-type expectations — precisely the ones most likely to
        # silently regress to a zero-asset run.
        total_read = as_count(
            await atlas.count_total_assets(client, self.connection_qualified_name)
        )

        # Three answers, not two. ``expect_lineage`` is graded on "at least one
        # Process exists", so an unreadable count folded into False fails the run
        # with "no lineage rows reached Atlas" — the connector's fault, asserted
        # by a run that never looked. It is the same C4 shape as the counts, and
        # it gets the same treatment: a settled zero stays an assertion failure,
        # an unreadable read is ungraded.
        lineage_read: bool | Unreadable | None = None
        if dag.expect_lineage:
            lineage = await atlas.count_lineage(
                client, self.connection_qualified_name, probe_types
            )
            if isinstance(lineage, Settled):
                lineage_read = any(count > 0 for count in lineage.value.values())
                logger.info(
                    "Lineage inventory under %s: %s lineage_present=%s",
                    self.connection_qualified_name,
                    dict(lineage.value),
                    lineage_read,
                )
            else:
                lineage_read = Unreadable(cause=lineage.cause)
                logger.warning(
                    "Lineage counts under %s could not be read, so the lineage "
                    "assertion is not graded",
                    self.connection_qualified_name,
                    exc_info=lineage.cause,
                )

        # Sample qualifiedNames for the location/hierarchy assertion (opt-in).
        # Only the declared types are probed, so connectors that don't set
        # expected_asset_qn_depth pay no extra Atlas call.
        sample_reads: Mapping[str, SampleRead] = {}
        if dag.expected_asset_qn_depth:
            sample_reads = as_samples(
                await atlas.sample_qualified_names(
                    client,
                    self.connection_qualified_name,
                    tuple(dag.expected_asset_qn_depth),
                ),
                tuple(dag.expected_asset_qn_depth),
            )
            logger.info(
                "Atlas qualifiedName samples under %s: %s",
                self.connection_qualified_name,
                dict(sample_reads),
            )

        return self._outcome(
            ae_result,
            connection_in_atlas=True,
            connection_read=connection_read,
            count_reads=count_reads,
            total_read=total_read,
            lineage_read=lineage_read,
            sample_reads=sample_reads,
        )

    async def _poll_asset_counts(
        self, client: AsyncAtlanClient, probe_types: tuple[str, ...]
    ) -> Mapping[str, CountRead]:
        """Poll per-type counts until the declared expectations are met.

        Elasticsearch is eventually consistent but assets appear within seconds
        if publish succeeded, so this uses the short dedicated budget rather than
        the wide one the connection poll gets.

        Args:
            client: The open Atlas client.
            probe_types: Types to count.

        Returns:
            The last reading, per type — an
            :class:`~application_sdk.testing.harness.expectations.Unreadable` for
            a search that could not be read.
        """
        last: Mapping[str, CountRead] = dict.fromkeys(probe_types, 0)

        async def _probe() -> Mapping[str, CountRead]:
            nonlocal last
            last = as_counts(
                await atlas.count_assets(
                    client, self.connection_qualified_name, probe_types
                ),
                probe_types,
            )
            # conformance: ignore[L006] short, bounded poll (atlas_asset_poll_timeout_seconds) with modest iteration count, not a hot loop; the per-iteration asset counts are the primary diagnostic signal when an E2E run fails to converge
            logger.info(
                "Atlas inventory under %s: %s",
                self.connection_qualified_name,
                dict(last),
            )
            return last

        def _met(counts: Mapping[str, CountRead]) -> bool:
            # Single source of truth: the polling exit reuses the same evaluator
            # as the final assertion, so the two can never drift to different
            # definitions of "met". Floors and the non-empty backstop can exit as
            # soon as they're satisfied; exact-count parity must NOT, because ES
            # indexing is eventually consistent and a transient match could end
            # polling before late-arriving over-extracted assets land. Keeping
            # the loop alive to the deadline is what surfaces over-extraction.
            if self._dag.expected_exact_counts:
                return False
            return not self._asset_findings(counts)

        await poll_until(
            _probe,
            settled=_met,
            budget=self._atlas_counts_budget(),
            label=f"Atlas asset counts under {self.connection_qualified_name}",
        )
        return last

    def _outcome(
        self,
        ae_result: DAGRunResult,
        *,
        connection_in_atlas: bool,
        connection_read: Outcome[bool] | None = None,
        count_reads: Mapping[str, CountRead] | None = None,
        total_read: CountRead | None = None,
        lineage_read: bool | Unreadable | None = None,
        sample_reads: Mapping[str, SampleRead] | None = None,
    ) -> FullDAGOutcome:
        """Assemble the outcome, projecting each reading into its settled half.

        Args:
            ae_result: The DAG snapshot.
            connection_in_atlas: Whether the Connection was observed.
            connection_read: The Connection poll's verdict, when it ran.
            count_reads: Per-type counts as read.
            total_read: All-types total as read.
            lineage_read: Whether any lineage asset was observed, or the fact
                that the count could not be read.
            sample_reads: Sampled qualified names as read.

        Returns:
            The outcome. Every reading is carried in both shapes: the settled
            projection a connector suite indexes, and the reading the assertion
            ladder grades.
        """
        counts = dict(count_reads or {})
        samples = dict(sample_reads or {})
        return FullDAGOutcome(
            ae_result=ae_result,
            connection_qualified_name=self.connection_qualified_name,
            connection_in_atlas=connection_in_atlas,
            asset_counts={
                name: value for name, value in counts.items() if isinstance(value, int)
            },
            total_assets=total_read if isinstance(total_read, int) else 0,
            lineage_present=lineage_read is True,
            connection_read=connection_read,
            lineage_read=lineage_read,
            asset_qn_samples={
                name: list(value)
                for name, value in samples.items()
                if not isinstance(value, Unreadable)
            },
            asset_count_reads=counts,
            total_asset_read=total_read,
            asset_qn_reads=samples,
            connection_expected=self._dag.expect_connection,
        )

    # ------------------------------------------------------------------
    # Asset-output expectations (floors + exact parity + non-empty)
    # ------------------------------------------------------------------

    def _asset_expectations(self) -> AssetExpectations:
        """This suite's declarations, as the shared evaluators take them.

        Returns:
            The floors, exacts, depths and backstop this class declares. Turning
            the ``ClassVar``\\s into a value is the whole of what generalising
            these two checks required — a composer that is not a subclass can
            then grade the same expectations.
        """
        dag = self._dag
        return AssetExpectations(
            floors=dict(dag.expected_min_asset_counts),
            exacts=dict(dag.expected_exact_counts),
            depths=dict(dag.expected_asset_qn_depth),
            require_nonempty=dag.require_nonempty_assets,
            # getattr, because the count half of this is a pure function of the
            # class attributes and is unit-tested on an instance that never ran
            # setup_method. An empty prefix makes the location half a no-op,
            # which is exactly what "there is no connection to measure depth
            # from" should do.
            connection_qualified_name=getattr(self, "connection_qualified_name", ""),
        )

    def _asset_findings(
        self,
        counts: Mapping[str, CountRead],
        *,
        total_assets: CountRead | None = None,
    ) -> Sequence[Finding]:
        """Grade the counts, keeping "could not read" apart from "read zero".

        Args:
            counts: Per-type counts as read.
            total_assets: The all-types total as read, or ``None`` to fall back
                to the sum of the per-type counts.

        Returns:
            One finding per unmet expectation. A finding whose
            :attr:`~application_sdk.testing.harness.expectations.Finding.expectation`
            is
            :data:`~application_sdk.testing.harness.expectations.UNREADABLE` says
            the check could not be graded — never that the connector regressed.
        """
        return evaluate_counts(
            counts, self._asset_expectations(), total_assets=total_assets
        )

    def _location_findings(
        self, samples: Mapping[str, SampleRead]
    ) -> Sequence[Finding]:
        """Grade the sampled qualified names against the declared depths.

        Args:
            samples: Sampled names per type, as read.

        Returns:
            One finding per sample that is not nested under the connection, or is
            nested at the wrong depth.
        """
        return evaluate_locations(samples, self._asset_expectations())

    def _evaluate_asset_expectations(
        self,
        asset_counts: Mapping[str, CountRead],
        *,
        total_assets: CountRead | None = None,
    ) -> list[str]:
        """Evaluate per-type asset expectations against the Atlas counts.

        Kept on this class, with this name and this return type, because
        connector suites call it. The logic itself is
        :func:`~application_sdk.testing.harness.expectations.evaluate_counts`,
        which is a pure function of its inputs and needs no tenant to test:

          * ``expected_min_asset_counts`` — floors (``>=``).
          * ``expected_exact_counts`` — exact parity (``==``) vs the
            direct-run baseline; catches under- AND over-extraction.
          * ``require_nonempty_assets`` — a COMPLETED run that lands zero
            assets fails (the "completed but extracted nothing" backstop).

        Args:
            asset_counts: Per-type counts. Plain ints are the ordinary case; an
                :class:`~application_sdk.testing.harness.expectations.Unreadable`
                marks a type whose search could not be read, which produces a
                finding saying the check went ungraded rather than one claiming
                the count was low.
            total_assets: The true count across ALL asset types (from
                ``count_total_assets``). When omitted it falls back to
                ``sum(asset_counts.values())`` so unit tests can drive the pure
                logic. The backstop uses it so it fires even for connectors that
                declare no per-type expectations — the ones most likely to
                silently regress — UNLESS the connector opts out
                (``require_nonempty_assets = False``) or its declared
                expectations themselves assert zero.

        Returns:
            Human-readable failure lines, empty when every expectation was met.
        """
        return [
            _render_finding(finding)
            for finding in self._asset_findings(asset_counts, total_assets=total_assets)
        ]

    def _validate_asset_locations(
        self, asset_qn_samples: Mapping[str, SampleRead]
    ) -> list[str]:
        """Validate sampled assets sit under the connection at the declared depth.

        Kept on this class for the reason :meth:`_evaluate_asset_expectations`
        is; the logic is
        :func:`~application_sdk.testing.harness.expectations.evaluate_locations`.
        For each declared type, every sampled qualifiedName must (a) be nested
        under the connection prefix and (b) have exactly the declared number of
        path segments below it — catching a whole type that published to the
        wrong hierarchy level (mis-parented / flattened / a dropped path-template
        segment), which the counts alone can't see.

        .. note::
           This used to fail **open**. The sampling read returned ``[]`` on any
           search error, a type with an empty sample is skipped, and so an auth
           or API fault was graded as a pass. It cannot any more: an unreadable
           sample arrives here as
           :class:`~application_sdk.testing.harness.expectations.Unreadable` and
           produces a finding of its own. That is finding C4 on FND-224, and the
           fix is structural — "I could not read" no longer has the same
           spelling as "nothing to check".

        Args:
            asset_qn_samples: Sampled qualified names per type, as read.

        Returns:
            Human-readable failure lines, empty when every sample was fine.
            Types with no sampled assets are still skipped: "too few / none" is
            the COUNT check's job, so this check is only about the *shape* of
            assets that did land.
        """
        return [
            _render_finding(finding)
            for finding in self._location_findings(asset_qn_samples)
        ]

    # ------------------------------------------------------------------
    # Worker-up-only tier (no source provisioned)
    # ------------------------------------------------------------------

    def assert_worker_up(self) -> None:
        """Assert only that the app worker deployed and serves its health endpoint.

        The no-source tier: when a connector has no extraction source in CI
        (``source_available`` False), the full-DAG e2e can't extract, so it
        proves the worker came up instead — a GET of ``/server/health`` returns
        2xx. This is a hard assertion, so an unhealthy worker fails RED. The
        *caller* (``test_full_dag_runs_end_to_end``) then raises ``pytest.skip``
        so a healthy worker reports SKIPPED, not a green pass — because the full
        DAG was never exercised. The sdr-e2e CI action already gates on the same
        endpoint before pytest; re-asserting it here keeps a bare local
        ``pytest`` meaningful as a worker-deploy smoke check.

        Raises:
            WorkerNotHealthyError: The worker never answered 2xx inside
                ``worker_health_timeout_seconds``. The last failure seen is a
                *field* rather than a fragment of a sentence, so a refused
                connection and a 503 — which point at different halves of a
                deployment — are told apart without parsing prose. It **is** an
                ``AssertionError`` as well, so a connector suite's existing
                ``except AssertionError`` still catches it.
        """
        run_sync(self._assert_worker_up_async())

    def _worker_health_transport(self) -> httpx.AsyncBaseTransport | None:
        """HTTP transport the worker-health probe runs on.

        Returns:
            ``None`` — the real network. Overridden by a suite that drives the
            probe against a scripted transport, which is the seam
            :func:`~application_sdk.testing.harness.preconditions.check_worker_health`
            offers so the poll's own behaviour is exercised through a real
            :class:`httpx.AsyncClient` rather than against a patched module
            global.
        """
        return None

    async def _assert_worker_up_async(self) -> None:
        """Run the worker-health precondition and raise this package's leaf for it.

        The probe is
        :func:`~application_sdk.testing.harness.preconditions.check_worker_health`,
        run through the same gate a composing suite uses — so the connector tier
        and the runtime tier poll the same endpoint the same way and report the
        same reading. What stays here is the *leaf*: a
        ``WorkerNotHealthyError`` naming the URL, the attempts and the last
        transport error is remediation advice a generic
        ``PreconditionsFailedError`` carrying a label cannot give, and losing it
        would be a behaviour change dressed as a lift.

        Raises:
            WorkerNotHealthyError: The endpoint never answered 2xx in budget.
        """
        url = os.environ.get("E2E_WORKER_HEALTH_URL", self.worker_health_url)
        logger.info("Worker-up-only tier: probing %s", url)
        report = await run_preconditions(
            [
                check_worker_health(
                    url,
                    budget=self._worker_health_budget(),
                    label=f"{self.connector_short_name} worker health at {url}",
                    transport=self._worker_health_transport(),
                )
            ]
        )
        outcome = report.outcomes[0]
        if isinstance(outcome, Settled):
            logger.info("App worker healthy: %s -> %s", url, outcome.value)
            return
        last: HealthReading | None = getattr(outcome, "last", None)
        last_error = str(last) if last is not None else ""
        raise WorkerNotHealthyError(
            message=(
                f"App worker for {self.connector_short_name} did not "
                f"become healthy at {url} within "
                f"{self.worker_health_timeout_seconds}s "
                f"({outcome.attempts} attempts, "
                f"{outcome.elapsed.total_seconds():.0f}s elapsed; "
                f"last: {last_error}). No source is provisioned, so this run "
                "only checks that the worker deploys and serves /server/health."
            ),
            url=url,
            attempts=outcome.attempts,
            elapsed_seconds=outcome.elapsed.total_seconds(),
            last_error=last_error,
            cause=getattr(outcome, "cause", None),
        )

    # ------------------------------------------------------------------
    # Default test method
    # ------------------------------------------------------------------

    def test_full_dag_runs_end_to_end(self) -> None:
        """Submit, run, assert success — once per declared DAG run.

        Calls :meth:`seed_prerequisites` first (a no-op unless the suite
        overrides it), so an entrypoint that consumes state rather than creating
        it — a query-history miner — can put that state in place without
        replacing this whole method and losing the assertion ladder below.

        Then one run per entry in :attr:`dag_runs`, in order, against the one
        connection this suite minted — or, when ``dag_runs`` is empty (the
        default), the single implicit run against the class attributes that is
        all this method ever did. Each run is graded on its own through
        :meth:`assert_dag_outcome` as soon as it finishes, and the first failure
        stops the sequence: a mine whose prerequisite crawl did not land has
        nothing left to prove, and running it anyway would report the crawl's
        failure as the miner's.

        Asserts (in order), per run:
          1. Every DAG node succeeded.
          2. The Connection asset exists in Atlas.
          3. Asset-count expectations: ``expected_min_asset_counts`` floors,
             ``expected_exact_counts`` parity vs. the direct-run baseline, and
             the non-empty backstop (see ``_evaluate_asset_expectations``).
          4. Asset locations: sampled assets are nested under the connection at
             the depth declared in ``expected_asset_qn_depth`` (opt-in).
          5. At least one Process/ColumnProcess exists (unless ``expect_lineage``
             is False).

        Assertions 2-5 are all about published inventory, so ``expect_connection
        = False`` drops them and leaves assertion 1 as the verdict. That is the
        whole gate for an entrypoint that publishes nothing; such a suite is
        expected to add its own terminal evidence on top. Every expectation the
        ladder reads is resolved per run, so a crawl declared inside a miner
        suite is graded as a crawl.

        When no extraction source is provisioned (``source_available`` False),
        this degrades to a worker-up-only check — see :meth:`assert_worker_up`.
        The full DAG is NOT exercised, so this run must not report a green
        *pass* that reads as "full-DAG e2e passed": after asserting the worker
        is healthy (an unhealthy worker still fails RED), it raises
        ``pytest.skip`` so the check surface shows SKIPPED, not passed. That
        distinction matters — a connector could regress its entire
        extract->publish path and, without the skip, CI would stay green
        purely because a source wasn't provisioned.
        """
        if not self.source_available:
            # Worker health is still a hard precondition (raises AssertionError
            # => RED) so a broken worker is never masked. But a healthy worker
            # only proves the app deployed, not that extraction works — so mark
            # the run SKIPPED rather than passed.
            self.assert_worker_up()
            pytest.skip(
                f"No extraction source provisioned for {self.connector_short_name} "
                "(source_available=False): the app worker was verified healthy, but "
                "the full extract->publish->Atlas DAG was NOT exercised. This is a "
                "worker-up smoke check, not a full-DAG e2e pass. Provision a source "
                "(a CI container, or app-owner-supplied credentials) to run the full "
                "DAG."
            )

        # Put any state this entrypoint consumes in place before the DAG runs.
        # No-op by default; a miner suite creates the connection it enriches
        # here, under this test's own ephemeral QN so teardown purges it.
        self.seed_prerequisites()

        # One wrapper around the run and the whole assertion ladder, rather than
        # a collection call at each of the six exits. The exits are what a
        # future assertion is added *between*, and an evidence hook per exit is
        # one a new assertion silently does not get.
        #
        # ``dag_runs`` empty is the single-run path every suite had before
        # FND-1157: one implicit run against the ClassVars, graded once. A suite
        # that declares runs gets each one submitted, polled and graded in
        # order, against the SAME seeded connection — and stops at the first
        # that fails, because a run whose prerequisite did not produce what it
        # consumes has nothing left to prove.
        self.dag_outcomes = []
        for spec in self.dag_runs or (None,):
            # Inside the run's own block, so the bundle names the run that
            # failed rather than the class's default — on a suite running
            # several DAGs that is the only thing in it that says which.
            # ``outcome`` is per iteration for the same reason: a later run's
            # failure must not be evidenced with an earlier run's readings.
            with self._dag_run(spec) as dag:
                outcome: FullDAGOutcome | None = None
                try:
                    outcome = self.run_full_dag()
                    self.dag_outcomes.append(outcome)
                    self.assert_dag_outcome(dag, outcome)
                # conformance: ignore[E004] re-raised unchanged on the next line; this collects evidence for a failure it does not handle, and swallowing it would replace a real verdict with a green run
                except Exception as failure:
                    self._collect_failure_evidence(failure, outcome)
                    raise

    def assert_dag_outcome(self, dag: ResolvedDAG, outcome: FullDAGOutcome) -> None:
        """Grade one run of one entrypoint DAG.

        Called once per run, in order, with the run that produced *outcome*
        already resolved — so the default ladder below grades a crawl against
        the crawl's expectations and a mine against the mine's, and never
        against a merged verdict over both.

        Override it on a suite whose runs need different assertions than the
        ladder gives, branching on ``dag.label``; call ``super()`` for the runs
        that want the ladder. The outcomes accumulate on ``self.dag_outcomes``
        in run order, for a suite that wants to assert across them afterwards.

        Args:
            dag: The run being graded, with every field settled.
            outcome: What that run produced.

        Raises:
            AssertionError: On the first unmet expectation — see
                :meth:`_assert_full_dag_outcome`.
        """
        self._assert_full_dag_outcome(outcome)

    def _assert_full_dag_outcome(self, outcome: FullDAGOutcome) -> None:
        """The assertion ladder, split out so one wrapper can cover all of it.

        Args:
            outcome: What the run produced.

        Raises:
            AssertionError: On the first unmet expectation, in the documented
                order. Split from :meth:`test_full_dag_runs_end_to_end` purely so
                the evidence wrapper there has one body to guard; the sequence
                and the messages are unchanged.
        """
        # Ungraded before unmet, and before the Connection gate below. A poll
        # that could not be read is not the Connection failing to land, and the
        # gate cannot tell the two apart from a boolean.
        self._raise_if_ungraded(self._unreadable_probe_findings(outcome))

        # The Connection clause only applies to an entrypoint that publishes
        # inventory. With expect_connection False the Atlas probes never ran, so
        # consulting connection_in_atlas here would fail every such run.
        dag_ok = self._core_dag_ok(outcome.ae_result)
        if not (
            dag_ok and (outcome.connection_in_atlas or not self._dag.expect_connection)
        ):
            ae_result = outcome.ae_result
            nodes_msg = (
                self._describe_dag_nodes(ae_result)
                if ae_result.failed_nodes
                else "  (all DAG nodes succeeded; Connection just didn't land in Atlas)"
            )
            # Reporting the connection line on a suite that never probed for one
            # would send the reader hunting a connection that was never meant to
            # exist. Say so instead.
            connection_line = (
                f"Connection in Atlas? {outcome.connection_in_atlas}\n"
                if self._dag.expect_connection
                else "Connection in Atlas? not applicable (expect_connection=False)\n"
            )
            raise AssertionError(
                f"Full-DAG e2e failed for connector={self.connector_short_name}\n"
                f"AE run_id={ae_result.run_id} slug={ae_result.workflow_slug}\n"
                f"{self._dag_outcome_headline(ae_result)}\n"
                f"{connection_line}"
                f"DAG nodes:\n{nodes_msg}"
            )

        if not self._dag.expect_connection:
            # Assertions 2-5 are all about published inventory, and the probes
            # that feed them never ran. Evaluating them against empty counts
            # would fail every run — the zero-asset backstop in
            # _evaluate_asset_expectations most obviously. The DAG gate above is
            # the verdict; anything further is the suite's own to assert.
            logger.info(
                "%s declares expect_connection=False — DAG succeeded; skipping the "
                "asset-count, asset-location and lineage assertions",
                type(self).__name__,
            )
            return

        count_findings = self._asset_findings(
            outcome.asset_count_reads or outcome.asset_counts,
            total_assets=(
                outcome.total_asset_read
                if outcome.total_asset_read is not None
                else outcome.total_assets
            ),
        )
        location_findings = self._location_findings(
            outcome.asset_qn_reads or outcome.asset_qn_samples
        )
        # Ungraded before unmet, always. A finding that exists because a search
        # could not be READ is not evidence about the connector, and reporting it
        # as one is what sent an Atlas outage to the connector team as "the
        # floors were not met". It is raised first, and as a leaf that is not an
        # AssertionError, so pytest marks the leg an error rather than a failure.
        self._raise_if_ungraded([*count_findings, *location_findings])

        asset_failures = [_render_finding(finding) for finding in count_findings]
        if asset_failures:
            raise AssertionError(
                "Atlas inventory under "
                f"{outcome.connection_qualified_name} did not meet expectations:\n"
                + "\n".join(asset_failures)
                + f"\nFull counts: {outcome.asset_counts}"
            )

        location_failures = [_render_finding(finding) for finding in location_findings]
        if location_failures:
            raise AssertionError(
                "Published assets are at the wrong location under "
                f"{outcome.connection_qualified_name} (extract succeeded and the "
                "counts may look right, but the qualifiedName hierarchy is "
                "wrong):\n" + "\n".join(location_failures)
            )

        # `lineage_present` is False for both "no Process exists" and "never
        # probed"; an unreadable read can no longer reach here, because
        # _unreadable_probe_findings raised on it above.
        if self._dag.expect_lineage and not outcome.lineage_present:
            raise AssertionError(
                "No lineage Process/ColumnProcess assets found under "
                f"{outcome.connection_qualified_name}. The DAG's qi + "
                "lineage-app + lineage-publish nodes reported success but "
                "no lineage rows reached Atlas."
            )

    def _unreadable_probe_findings(self, outcome: FullDAGOutcome) -> Sequence[Finding]:
        """Findings for the two probes whose verdict is not a per-type count.

        The Connection poll and the lineage count are graded as booleans, so
        neither has a place in :meth:`_asset_findings` — and both used to lose
        the difference between "it is not there" and "I could not look".

        Only :class:`~application_sdk.testing.harness.outcome.Indeterminate` is
        ungraded here, which is narrower than "every non-settled verdict".
        :class:`~application_sdk.testing.harness.outcome.NeverStarted` and
        :class:`~application_sdk.testing.harness.outcome.Expired` on the
        Connection poll mean the Connection never materialised inside the budget
        — a real finding about the publish path, and one the existing
        ``AssertionError`` states well. Widening the ungraded set to include them
        would turn a genuine regression into "could not tell".

        Args:
            outcome: What the run produced.

        Returns:
            Zero, one or two findings carrying
            :data:`~application_sdk.testing.harness.expectations.UNREADABLE`.
        """
        findings: list[Finding] = []
        connection = outcome.connection_read
        if isinstance(connection, Indeterminate):
            findings.append(
                Finding(
                    subject="Connection",
                    detail=(
                        f"could not be read, so whether "
                        f"{outcome.connection_qualified_name} landed was not "
                        f"graded: {type(connection.cause).__name__}: "
                        f"{connection.cause}"
                    ),
                    expectation=UNREADABLE,
                )
            )
        lineage = outcome.lineage_read
        if isinstance(lineage, Unreadable):
            findings.append(
                Finding(
                    subject="lineage",
                    detail=(
                        "could not be read, so the lineage expectation was not "
                        f"graded: {type(lineage.cause).__name__}: {lineage.cause}"
                    ),
                    expectation=UNREADABLE,
                )
            )
        return findings

    def _raise_if_ungraded(self, findings: Sequence[Finding]) -> None:
        """Fail as a dependency fault when an expectation could not be graded.

        Args:
            findings: Every finding the two evaluators produced.

        Raises:
            AtlasReadIndeterminateError: At least one finding carries
                :data:`~application_sdk.testing.harness.expectations.UNREADABLE`,
                meaning the reading behind it was never taken. Deliberately not
                an ``AssertionError``: the run has no observation to make a claim
                about the connector with, and pytest reports an error rather than
                a failure so the leg cannot be read as a regression.
        """
        ungraded = [
            finding for finding in findings if finding.expectation == UNREADABLE
        ]
        if not ungraded:
            return
        raise AtlasReadIndeterminateError(
            message=(
                "Atlas could not be read under "
                f"{self.connection_qualified_name}, so "
                f"{len(ungraded)} expectation(s) went ungraded. This is not a "
                "verdict on the connector — the run never saw what it landed:\n"
                + "\n".join(_render_finding(finding) for finding in ungraded)
            ),
            checks=",".join(sorted({finding.subject for finding in ungraded})),
        )

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def _collect_failure_evidence(
        self, failure: BaseException, outcome: FullDAGOutcome | None
    ) -> None:
        """Write a redacted evidence bundle for a failed run.

        Called on every path out of :meth:`test_full_dag_runs_end_to_end` that is
        not a pass, including the ones where ``run_full_dag`` itself raised and
        there is no outcome to describe — which are the runs that most need the
        AE identity recorded somewhere a person can find it after the job is
        gone.

        Best-effort by construction, and that is not a hedge: this runs inside an
        ``except`` block whose job is to re-raise a real verdict. A collector
        that raised here would replace the failure being diagnosed with its own,
        which is the exact miscue :meth:`teardown_method` is written to avoid.

        **No pod logs on this path, and that is not an omission.** The connector
        CI leg has no ``kubectl`` route into the tenant vcluster — that is the
        same constraint that makes the AE submit the only tenant-facing probe of
        the installed app pod — so the pod half of an evidence bundle is not
        collectable from here. The container that *is* local is dumped by the CI
        action into the same ``results/`` directory this writes into
        (``sdr-container.log``), so the two land in one artifact.

        Args:
            failure: What went wrong. Its formatted traceback is written as an
                artifact, secret-redacted like everything else.
            outcome: The run's outcome when there was one, else ``None``.
        """
        if not self.evidence_dir:
            return
        try:
            bundle = self._failure_evidence(failure, outcome)
            written = write_bundle(
                bundle,
                Path(self.evidence_dir) / type(self).__name__,
                secrets=secrets_from_environment(
                    os.environ,
                    # Not credential-shaped by name and not a credential by
                    # value, but a tenant hostname identifies a customer
                    # environment and this bundle is uploaded and retained.
                    also=("ATLAN_BASE_URL",),
                ),
            )
            if written:
                logger.info(
                    "e2e evidence: wrote %d file(s) under %s",
                    len(written),
                    Path(self.evidence_dir) / type(self).__name__,
                )
        # conformance: ignore[E004] evidence boundary — this runs inside an except block re-raising the real verdict, and a collector failure must never replace it
        except Exception:
            logger.warning(
                "e2e evidence: could not write the failure bundle — the run's own "
                "verdict is unaffected",
                exc_info=True,
            )

    def _failure_evidence(
        self, failure: BaseException, outcome: FullDAGOutcome | None
    ) -> EvidenceBundle:
        """Build the bundle for *failure*, from whatever the run got as far as.

        Args:
            failure: What went wrong.
            outcome: The run's outcome when there was one, else ``None``.

        Returns:
            The bundle, unredacted — redaction is :func:`write_bundle`'s, at the
            boundary that ships it.
        """
        findings = [
            Finding(
                subject=type(failure).__name__,
                detail=str(failure),
                expectation="full-dag e2e",
            )
        ]
        readings: dict[str, object] = {
            "connector": self.connector_short_name,
            "entrypoint": self._resolved_entrypoint(),
            # Which of the suite's runs this was. Identical to the entrypoint on
            # a single-DAG suite; on a suite running several, it is the only
            # thing in the bundle that says which run failed.
            "dag": self._dag.label,
            "dag_runs_completed": len(getattr(self, "dag_outcomes", ())),
            "mode": self.mode.value,
            "run_id": self.run_id,
            "connection_qualified_name": getattr(self, "connection_qualified_name", ""),
            "extract_task_queue": self._extract_task_queue(),
            "seed_version": self._seed_version,
        }
        if outcome is not None:
            ae_result = outcome.ae_result
            readings.update(
                {
                    "ae_run_id": ae_result.run_id,
                    "ae_workflow_slug": ae_result.workflow_slug,
                    "ae_status": ae_result.status.value,
                    # The per-node table is the single most-read part of a failed
                    # leg, so it goes in the machine-readable half rather than
                    # only into the rendered text below.
                    "dag_nodes": [
                        {
                            "name": node.name,
                            "status": node.status.value,
                            "error_message": node.error_message,
                            "duration_seconds": node.duration_seconds,
                            # The queue the *seed* asked for, not a guarantee of
                            # what ran — Heracles re-fetches the tenant's own
                            # manifest at submit. See :class:`NodeDispatch`.
                            "dispatch_requested_to": (
                                dispatch.task_queue
                                if (dispatch := self._node_dispatch.get(node.name))
                                else None
                            ),
                        }
                        for node in ae_result.nodes
                    ],
                    "connection_in_atlas": outcome.connection_in_atlas,
                    "asset_counts": dict(outcome.asset_counts),
                    "total_assets": outcome.total_assets,
                    "lineage_present": outcome.lineage_present,
                }
            )
        artifacts = {"traceback.txt": safe_traceback(failure)}
        if outcome is not None:
            artifacts["dag-nodes.txt"] = (
                f"{self._dag_outcome_headline(outcome.ae_result)}\n"
                f"{self._describe_dag_nodes(outcome.ae_result)}"
            )
        return EvidenceBundle(
            label=f"{type(self).__name__} — {self.connector_short_name}",
            findings=tuple(findings),
            readings=readings,
            artifacts=artifacts,
        )
