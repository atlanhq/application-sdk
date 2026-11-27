"""Module-level types, constants and pure helpers of :mod:`application_sdk.testing.e2e.base`.

Split out of ``base`` (FND-4267) so the mixins ``BaseE2ETest`` is composed from
can construct them without importing ``base``. Every name here is re-exported
from :mod:`application_sdk.testing.e2e.base`, which stays the import path to use.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from application_sdk.testing.e2e._errors import (
    InvalidLineageEdgeDeclarationError,
)
from application_sdk.testing.harness.automation_engine import (
    DAGNodeStatus,
    DAGRunResult,
)
from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    AssetAttributes,
    AssetRef,
    AttributeExpectationValue,
    AttributeSampleRead,
    CountRead,
    Finding,
    LineageEdge,
    LineageEdgeRead,
    SampleRead,
    Unreadable,
)
from application_sdk.testing.harness.outcome import (
    Outcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    from application_sdk.execution._temporal.store_assert import StoreAssertOutput
    from application_sdk.testing.harness.temporal import PollerInfo


# Where the sdr-e2e composite action selects the CI Dapr components, and the
# name the atlan-configurator emits the tenant blobstorage binding under. Both
# are the CI convention rather than a rule, which is why each has an env
# override (``E2E_SEED_COMPONENTS_DIR`` / ``E2E_SEED_STORE_BINDING``) and the
# whole resolution sits behind an overridable method — a leg whose layout
# differs is a per-leg env var, not an edit here.
_DEFAULT_SEED_COMPONENTS_DIR = "ci-deploy/components"
_DEFAULT_SEED_STORE_BINDING = "atlan-objectstore"

# The mustache key the toolkit's ``ConnectionSelector`` input fills — the
# connection a utility DAG (connection-delete, a miner) acts on rather than
# creates. Teardown reads it to tell a submit against another connection from
# one that may mint the run's own; see
# BaseE2ETest._submit_may_create_connection.
_CONNECTION_SELECTOR_SUBSTITUTION = "{{connection-qualified-name}}"

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
    "attribute": "  - {subject} {detail}",
    "lineage_edge": "  - {subject}: {detail}",
    "missing": "  - {subject}: {detail}",
    "ambiguous": "  - {subject}: {detail}",
    UNREADABLE: "  - {subject}: {detail}",
}


#: A lineage-edge type name: what Atlas accepts as a typeName.
_EDGE_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _parse_lineage_edge(key: str) -> LineageEdge:
    """Split one ``expected_lineage_edges`` key into (source, target) types.

    Args:
        key: ``"SourceType -> TargetType"``; whitespace around ``->`` is
            ignored.

    Returns:
        The two type names.

    Raises:
        InvalidLineageEdgeDeclarationError: The key is not exactly two valid
            type names joined by one ``->``.
    """
    parts = [part.strip() for part in key.split("->")]
    if len(parts) != 2 or not all(_EDGE_TYPE_NAME.match(part) for part in parts):
        raise InvalidLineageEdgeDeclarationError(
            message=(
                f"expected_lineage_edges key {key!r} is not of the form "
                "'SourceType -> TargetType' (two Atlan type names, letters, "
                "digits and underscores, joined by '->')."
            ),
            constraint="'SourceType -> TargetType'",
        )
    return LineageEdge(parts[0], parts[1])


def _normalise_lineage_edges(declared: Mapping[str, int]) -> dict[LineageEdge, int]:
    """Validate a lineage-edge declaration and parse its keys.

    Args:
        declared: ``"Source -> Target"`` -> minimum process count, as a suite
            writes it.

    Returns:
        The same floors keyed by the parsed edge.

    Raises:
        InvalidLineageEdgeDeclarationError: A key is malformed, two keys name
            the same edge, or a floor is not an integer >= 1.
    """
    normalised: dict[LineageEdge, int] = {}
    for key, floor in declared.items():
        edge = _parse_lineage_edge(key)
        if edge in normalised:
            raise InvalidLineageEdgeDeclarationError(
                message=(
                    f"expected_lineage_edges names the edge '{edge}' twice; "
                    "declare each edge once."
                ),
            )
        if isinstance(floor, bool) or not isinstance(floor, int) or floor < 1:
            raise InvalidLineageEdgeDeclarationError(
                message=(
                    f"expected_lineage_edges[{key!r}] is {floor!r}; the floor must "
                    "be an integer >= 1. A floor of zero asserts nothing — drop "
                    "the entry instead."
                ),
                constraint=">= 1",
            )
        normalised[edge] = floor
    return normalised


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


@dataclass(frozen=True, slots=True, kw_only=True)
class QueuePollerReading:
    """What Temporal answered when asked who is holding one dispatched queue.

    Recorded per task-queue name when the poll stops early, so a stuck-node
    line can say whether anything ever *claimed* the task rather than inferring
    it from AE's status. AE reports ``Running`` for a task that was dispatched
    and never claimed by any poller exactly as it does for one a worker picked
    up and stalled on (FND-1880), and the two need opposite investigations —
    "the worker/queue wiring is wrong" against "the activity is hanging".

    Attributes:
        task_queue: The queue asked about.
        namespace: Temporal namespace the read was scoped to.
        pollers: Every poller Temporal reported. **Empty is the finding, not a
            failed read**: a read that raised is never recorded at all (see
            :meth:`BaseE2ETest._read_queue_pollers`), so an empty tuple means
            Temporal answered "nobody is holding this queue".
    """

    task_queue: str
    namespace: str
    pollers: tuple[PollerInfo, ...]

    @property
    def unclaimed(self) -> bool:
        """Whether Temporal reported no poller at all on the queue.

        Returns:
            True when nothing is holding the queue, which is the observed form
            of "no worker ever claimed this task".
        """
        return not self.pollers

    @property
    def held_halves(self) -> str:
        """Which halves of the queue have pollers, as a clause.

        A task-queue *name* addresses two queues — workflow tasks and activity
        tasks — and the read covers both, so a non-empty result can mean either
        is held. Naming which is what makes the half-polling zombie legible: a
        worker whose workflow poll loop died while its activity loop lives holds
        one half, and a union count reports that as "1 poller" exactly as a
        healthy worker does. See :class:`TaskQueueType`.

        Returns:
            ``both halves`` when every half is held, else ``the <Half> half
            only``. Empty when :attr:`unclaimed`.
        """
        halves = {poller.task_queue_type for poller in self.pollers}
        if not halves:
            return ""
        if len(halves) > 1:
            return "both halves"
        return f"the {next(iter(halves)).value} half only"

    @property
    def identities(self) -> str:
        """The pollers as ``identity (Workflow, build X)``, comma-separated.

        Returns:
            One clause per poller, in the order Temporal reported them. Empty
            when :attr:`unclaimed` — no caller renders it in that case.
        """
        return ", ".join(
            f"{poller.identity} ({poller.task_queue_type.value}"
            + (f", build {poller.build_id}" if poller.build_id else "")
            + ")"
            for poller in self.pollers
        )


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
        asset_attribute_samples: A few sampled assets per type with the
            attribute values they carry (only for the types in
            ``expected_asset_attributes``); used to assert an asset's
            *contents*, which the counts and depths cannot see. Empty when
            attribute validation isn't requested or the Connection probe didn't
            succeed.
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
    asset_attribute_samples: dict[str, list[AssetAttributes]] = field(
        default_factory=dict
    )
    """Sampled assets and their attribute values, settled-only, for a connector
    suite that wants to assert something beyond the declared matchers."""
    asset_attribute_reads: Mapping[str, AttributeSampleRead] = field(
        default_factory=dict
    )
    """Sampled attribute values as the reader answered them, so an unreadable
    search is an
    :class:`~application_sdk.testing.harness.expectations.Unreadable` here rather
    than an empty sample the attribute check would skip. Same fail-open shape as
    :attr:`asset_qn_reads`, closed the same way."""
    addressed_attribute_reads: Mapping[AssetRef, AttributeSampleRead] = field(
        default_factory=dict
    )
    """Every asset matching each addressed ref's qualifiedName suffix, as read.

    Holds *all* the matches rather than the one the suite meant, because zero
    and several are the two answers that have to reach the grader intact: an
    addressed asset that is not there is a finding, and a suffix matching two
    assets is a different finding. Resolving either here would decide them
    silently."""
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
    store_assert_read: StoreAssertOutput | Unreadable | None = None
    """The object-store assertion node's verdict, the fact that it could not be
    read, or ``None`` when the run declared no store expectations. Set only by
    :class:`~application_sdk.testing.e2e.system_app.SystemAppE2ETest`."""
    lineage_edge_reads: LineageEdgeRead | None = None
    """Lineage processes per declared ``"Source -> Target"`` edge, as read, or
    the fact that they could not be; ``None`` when the suite declared no
    ``expected_lineage_edges``. What the ladder grades — a read that stopped at
    its scan cap carries that here, so an unreached floor is ungraded rather
    than failed."""
    lineage_edge_counts: dict[str, int] = field(default_factory=dict)
    """Settled projection of :attr:`lineage_edge_reads`, keyed by the declared
    edge as ``"Source -> Target"``. Empty when the read was not taken or could
    not be read — never zeros standing in for a failed search."""

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
        expected_asset_attributes: Per-type attribute-value claims.
        expected_asset_attributes_at: Per-asset attribute-value claims, keyed by
            type then qualifiedName suffix.
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
        expected_lineage_edges: Per-edge floors on lineage processes, keyed by
            ``"Source -> Target"`` type names. Last, so the fields before it
            keep their positional slots.
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
    expected_asset_attributes: (
        Mapping[str, Mapping[str, AttributeExpectationValue]] | None
    ) = None
    expected_asset_attributes_at: (
        Mapping[str, Mapping[str, Mapping[str, AttributeExpectationValue]]] | None
    ) = None
    connection_qualified_name: str | None = None
    label: str = ""
    expected_lineage_edges: Mapping[str, int] | None = None


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
    expected_asset_attributes: Mapping[str, Mapping[str, AttributeExpectationValue]] = (
        field(default_factory=dict)
    )
    expected_asset_attributes_at: Mapping[
        str, Mapping[str, Mapping[str, AttributeExpectationValue]]
    ] = field(default_factory=dict)
    connection_qualified_name: str = ""
    expected_lineage_edges: Mapping[LineageEdge, int] = field(default_factory=dict)
    """Validated and parsed once at resolution, so ``"A->B"`` and ``"A -> B"``
    resolve to the same edge."""
