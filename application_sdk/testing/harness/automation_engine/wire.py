"""The Automation Engine ``native-status`` wire shape, as typed values.

Lifted verbatim from ``testing/e2e/client.py`` (child F on FND-224). These are
the types FND-242 names as moving with the AE half — ``DAGNodeStatus``,
``DAGRunStatus``, ``DAGNodeResult``, ``DAGRunResult`` — plus the published-version
read and the defensive parsers they all depend on.

The scaffold in :mod:`application_sdk.testing.harness.automation_engine` sketched
a ``NativeStatus`` value with ``node_states`` / ``finished`` / ``fingerprint``.
It is not built: FND-242's own text assigns the *existing* wire types to this
move, and shipping a second vocabulary for one reading would leave every consumer
choosing between them. :attr:`DAGRunResult.fingerprint` supplies the third field
the sketch wanted, carried on the reading for exactly the reason the sketch gave
— so the string the watchdog compares and the string the report prints cannot
drift.

The shape of the response (captured against devex with a real workflow run):

.. code-block:: json

    {
      "status": "Running",
      "run_id": "7fd7b893-...",
      "workflow_slug": "mysql-oUUCLfTn",
      "temporal_run_id": "...",
      "dag_nodes": {
        "extract":         {"status": "Succeeded", "started_at": ..., "completed_at": ..., "error_message": null},
        "qi":              {"status": "Succeeded", ...},
        "publish":         {"status": "Succeeded", ...},
        "lineage-app":     {"status": "Running",   ...},
        "lineage-publish": {"status": "Pending",   ...}
      }
    }

We treat ``"Succeeded" | "Failed" | "Error" | "Cancelled"`` as terminal node
statuses, ``"Running" | "Pending" | "Scheduled"`` as in-flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

__all__ = [
    "DAGNodeResult",
    "DAGNodeStatus",
    "DAGRunResult",
    "DAGRunStatus",
    "PublishedVersion",
]


# Per-status glyphs for the poll-loop log line — gives the operator a
# quick visual scan of "what's done / what's running" without parsing
# a long ``a=Succeeded; b=Running; c=Pending`` string. Used by
# :func:`node_glyph` (per-node) and :data:`RUN_GLYPHS` (top-level).
# Colour emoji rather than monochrome glyphs: GH Actions logs render
# them inline and the colour signals status faster than the shape.
NODE_GLYPHS = {
    "Succeeded": "✅",
    "Failed": "❌",
    "Running": "🔄",
    "Pending": "🟡",
    "Cancelled": "🚫",
    "TimedOut": "⏰",
    "Skipped": "⏭️",
    "Omitted": "⊘",
}
RUN_GLYPHS = {
    "Succeeded": "✅",
    "Failed": "❌",
    "Running": "🔄",
    "Pending": "🟡",
    "Cancelled": "🚫",
    "TimedOut": "⏰",
    "Skipped": "⏭️",
}


class DAGNodeStatus(str, Enum):
    """Status values returned by ``native-status`` per DAG node."""

    PENDING = "Pending"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"
    # AE reports Skipped/Omitted for DAG nodes it intentionally did not run —
    # e.g. an opted-out DAG leg, the qi + lineage nodes when a crawl runs with
    # lineage disabled, or every downstream node once an upstream one fails.
    # These are terminal and NOT failures; the skip-tolerant gate
    # (BaseE2ETest._core_dag_ok) treats them as acceptable when lineage isn't
    # expected. Kept as explicit members so they no longer fall through
    # :func:`safe_node_status` to PENDING (which would hang the poll's "not
    # started" reasoning and false-fail all_nodes_succeeded). A skipped node
    # will not run without re-submission.
    SKIPPED = "Skipped"
    OMITTED = "Omitted"

    @property
    def is_terminal(self) -> bool:
        """True if this status will not change without re-submission."""
        return self in {
            DAGNodeStatus.SUCCEEDED,
            DAGNodeStatus.FAILED,
            DAGNodeStatus.ERROR,
            DAGNodeStatus.CANCELLED,
            DAGNodeStatus.SKIPPED,
            DAGNodeStatus.OMITTED,
        }

    @property
    def is_success(self) -> bool:
        """True when the node completed without error."""
        return self is DAGNodeStatus.SUCCEEDED

    @property
    def is_skipped(self) -> bool:
        """True when AE intentionally did not run the node (not a failure)."""
        return self in {DAGNodeStatus.SKIPPED, DAGNodeStatus.OMITTED}

    @property
    def is_not_started(self) -> bool:
        """True while AE has not reported the node as started.

        A node in this set has *not failed*, which is the distinction worth
        making in a diagnostic: a ``Failed`` node ran and errored, so reporting
        a ``Pending`` one the same way sends the operator looking for a bug in
        code that may never have executed.

        It does **not** prove the node was never dispatched. AE holds a node at
        ``Pending`` while its child workflow is running: on the run that
        motivated FND-708, ``lineage-publish`` read ``Pending`` for the whole
        poll while its child workflow had started 331ms in and was retrying an
        activity through repeated heartbeat timeouts. Only the child workflow's
        own history separates "nothing picked it up" from "picked up and stuck",
        so a message built on this must point there rather than assert a cause.
        """
        return self in {DAGNodeStatus.PENDING, DAGNodeStatus.SCHEDULED}


class DAGRunStatus(str, Enum):
    """Top-level status of an AE workflow run."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"
    # A run AE never executed — e.g. deduplicated against an in-flight run, or
    # every node opted out. Terminal: recognising it lets poll_native_status
    # return the true outcome immediately instead of mapping the unknown value
    # to PENDING and waiting out the full stall grace, which surfaces as a
    # misleading NoWorkerOnTaskQueueError.
    SKIPPED = "Skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DAGRunStatus.SUCCEEDED,
            DAGRunStatus.FAILED,
            DAGRunStatus.ERROR,
            DAGRunStatus.CANCELLED,
            DAGRunStatus.SKIPPED,
        }


@dataclass(frozen=True)
class DAGNodeResult:
    """One row of the per-node breakdown returned by ``native-status``."""

    name: str
    status: DAGNodeStatus
    started_at_ms: int | None
    completed_at_ms: int | None
    error_message: str | None

    @property
    def duration_seconds(self) -> float | None:
        """Wall time if both endpoints are populated."""
        if self.started_at_ms is None or self.completed_at_ms is None:
            return None
        return (self.completed_at_ms - self.started_at_ms) / 1000.0


@dataclass(frozen=True)
class DAGRunResult:
    """Full result of one ``native-status`` read."""

    run_id: str
    workflow_slug: str
    status: DAGRunStatus
    nodes: list[DAGNodeResult]
    # Set only on the observation ``poll_native_status`` returns because it hit
    # its own ceiling, so callers can say "the DAG did not complete in Xs"
    # instead of reporting the last-seen node states as node failures. ``None``
    # on every result that came back from a terminal run.
    timed_out_after_seconds: float | None = None
    # Set only on the observation attached to a ``DAGProgressStalledError``: the
    # progress watchdog stopped the poll early, so like
    # ``timed_out_after_seconds`` this is where the harness stopped watching
    # rather than a verdict. Carries the window that was breached.
    progress_stalled_after_seconds: float | None = None
    # Elapsed poll time since the last node-state transition, at the moment the
    # poll stopped watching — the ceiling or the progress watchdog. This is the
    # DAG-wide watchdog clock (the same quantity ``dag_progress_stall_seconds``
    # bounds), not a per-node age: it answers "how long has this DAG been frozen
    # in the state printed below".
    seconds_since_last_progress: float | None = None

    @property
    def fingerprint(self) -> str:
        """The node-glyph summary the progress watchdog compares.

        Carried on the reading rather than recomputed at each call site, so the
        string the watchdog compares and the string the report prints cannot
        drift — the one requirement the scaffold's ``NativeStatus`` sketch
        stated, kept here rather than in a parallel type.
        """
        return " ".join(node_glyph(n) for n in self.nodes)

    @property
    def all_nodes_succeeded(self) -> bool:
        return bool(self.nodes) and all(n.status.is_success for n in self.nodes)

    @property
    def timed_out(self) -> bool:
        """True when this observation is the poll loop's ceiling, not a verdict."""
        return self.timed_out_after_seconds is not None

    @property
    def progress_stalled(self) -> bool:
        """True when the progress watchdog ended the poll, not a verdict."""
        return self.progress_stalled_after_seconds is not None

    @property
    def stopped_watching(self) -> bool:
        """True when the poll stopped before the DAG reached a terminal state.

        The union of the two early exits — the poll ceiling
        (:attr:`timed_out`) and the progress watchdog
        (:attr:`progress_stalled`). Either way the node states are the last
        observation and not a verdict, which is the distinction a diagnostic
        has to make; the watchdog is now the one that fires first on any suite
        whose ceiling is at or below 1800s.
        """
        return self.timed_out or self.progress_stalled

    @property
    def failed_nodes(self) -> list[DAGNodeResult]:
        """Every node that did not succeed — failed *and* never-started alike.

        Kept as the wide "not successful" set the success gates gate on. Use
        :attr:`not_started_nodes` when the message needs to tell the operator
        which of the two happened.
        """
        return [n for n in self.nodes if not n.status.is_success]

    @property
    def not_started_nodes(self) -> list[DAGNodeResult]:
        """Nodes AE reports as not started (``Pending`` / ``Scheduled``).

        A status, not a dispatch fact — see
        :attr:`DAGNodeStatus.is_not_started`.
        """
        return [n for n in self.nodes if n.status.is_not_started]


@dataclass(frozen=True)
class PublishedVersion:
    """The workflow version AE currently serves as published, and its DAG.

    Read back after submit to see what Heracles published over the harness's
    seed version — the graph that actually executes. ``version`` is opaque
    beyond ``!=``: it exists so a caller can tell "AE superseded my seed" from
    "AE is still serving my seed", which is the difference between a real
    comparison and comparing the harness's own DAG to itself.

    Attributes:
        version: AE's version number, or ``None`` when the response omitted it
            (which makes the supersede question unanswerable, not answered no).
        dag: The version's ``dag`` object, ``{}`` when absent.
    """

    version: int | None
    dag: dict[str, Any]


def node_glyph(node: DAGNodeResult) -> str:
    """Format one node as ``glyph name`` for the poll-loop summary."""
    g = NODE_GLYPHS.get(node.status.value, "❔")
    # Trim long node names so the per-poll line stays scannable
    name = node.name.replace("lineage-publish", "lin-pub").replace(
        "lineage-app", "lin-app"
    )
    # Space between glyph and name — colour emoji renders wider than
    # the monochrome glyphs we used before, so the previous tight
    # "✓extract" lost legibility.
    return f"{g} {name}"


def first_version_row(body: Any) -> dict[str, Any] | None:
    """First version record in a ``/versions`` listing response, if any.

    The listing's envelope is not contractual — observed and plausible shapes
    put the rows under ``data`` directly, under a nested ``records`` /
    ``versions`` / ``items`` key, or return a bare single object for a
    ``page_size=1`` read. Accepting all of them keeps a shape change from
    reading as "the DAG does not match"; an envelope this function cannot parse
    returns ``None``, which callers treat as unanswerable rather than as a
    finding.
    """
    if isinstance(body, list):
        rows: Any = body
    elif isinstance(body, dict):
        rows = body.get("data", body)
        if isinstance(rows, dict):
            for key in ("records", "versions", "items"):
                nested = rows.get(key)
                if isinstance(nested, list):
                    rows = nested
                    break
    else:
        return None
    if isinstance(rows, dict):
        # A page_size=1 read that answered with the record itself.
        return rows if ("dag" in rows or "version" in rows) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                return row
    return None


def safe_node_status(raw: Any) -> DAGNodeStatus:
    """Map unknown / future status strings to ``Pending`` rather than raising.

    The AE service can introduce new intermediate statuses ahead of SDK
    releases; treating unknowns as non-terminal keeps polling alive
    instead of crashing the test on an unexpected enum value.
    """
    if not isinstance(raw, str):
        return DAGNodeStatus.PENDING
    try:
        return DAGNodeStatus(raw)
    except ValueError:
        logger.warning(
            "Unknown DAGNodeStatus value %r; returning PENDING", raw, exc_info=True
        )
        return DAGNodeStatus.PENDING


def safe_run_status(raw: Any) -> DAGRunStatus:
    """Same defensive mapping for the top-level run status."""
    if not isinstance(raw, str):
        return DAGRunStatus.PENDING
    try:
        return DAGRunStatus(raw)
    except ValueError:
        logger.warning(
            "Unknown DAGRunStatus value %r; returning PENDING", raw, exc_info=True
        )
        return DAGRunStatus.PENDING


def safe_int(raw: Any) -> int | None:
    """Cast a JSON number to int, returning None on missing / non-numeric."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Cannot cast %r to int; returning None", raw, exc_info=True)
        return None
