"""Run-polling layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

Polling a submitted DAG run, the queue-poller and deployed-manifest reads, and
describing its nodes and outcome for a failure message. Part of the linear
mixin chain described in :mod:`application_sdk.testing.e2e._base_config`
(FND-4267).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.errors.base import safe_traceback
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    DAGProgressStalledError,
    DeployedManifestMismatchError,
    NoWorkerOnTaskQueueError,
)
from application_sdk.testing.e2e._manifest_identity import (
    compare_node_identities,
    node_identities,
)
from application_sdk.testing.harness.automation_engine import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    PublishedVersion,
)
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.evidence import (
    EvidenceBundle,
    secrets_from_environment,
    write_bundle,
)
from application_sdk.testing.harness.expectations import (
    AssetRef,
    AttributeSampleRead,
    CountRead,
    Finding,
    LineageEdgeRead,
    SampleRead,
    Unreadable,
    sampled_qualified_names,
)
from application_sdk.testing.harness.outcome import (
    Outcome,
    Settled,
)
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    from application_sdk.testing.harness.temporal import (
        TemporalConnection,
    )

from application_sdk.testing.e2e._base_dag import _E2EDagMixin
from application_sdk.testing.e2e._base_types import (
    _HARD_FAIL_NODE_STATUSES,
    FullDAGOutcome,
    QueuePollerReading,
    _supersedes,
)

logger = get_logger(__name__)


class _E2EPollMixin(_E2EDagMixin):
    """Polling a DAG run and describing its nodes and outcome."""

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
          :attr:`~application_sdk.testing.harness.automation_engine.DAGNodeStatus.is_not_started`);
        * dispatched but never finished (``Running`` where the poll stopped) —
          which does not say whether a worker ever claimed it, so the line names
          the queue and hands off to :meth:`_claim_clause` for the cause;
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
                f"dispatched to {self._dispatch_note(node.name)}{stall_clause}. "
                f"{self._claim_clause(node.name, ae_result)}"
            )
        return f"  - {node.name}: status={node.status.value} error={node.error_message}"

    def _claim_clause(self, node_name: str, ae_result: DAGRunResult) -> str:
        """Whether anything ever *claimed* a still-Running node's task.

        The sentence this replaced asserted one of the two causes as fact — "a
        worker took it and stopped making progress (or died holding it)" — on
        the strength of AE reporting ``Running``. AE reports ``Running`` for a
        task that was dispatched and never claimed by any poller just as it does
        for one a worker picked up and stalled on, and the two need opposite
        investigations: a queue-name mismatch against a hanging activity. On the
        run behind FND-1880 the real cause was the first (the worker polled
        ``atlan-<app>-<deployment>`` while the harness dispatched to
        ``atlan-<app_with_underscores>-<deployment>``), and the assertion was
        read as evidence against that hypothesis — costing a round trip in the
        extraction path before it was reconsidered.

        Only the *negative* is decidable from a poller read: a queue with no
        pollers is the observed form of "nothing ever claimed it". The converse
        is not — ``DescribeTaskQueue`` answers who is holding the queue name
        now, not whether this node's task was ever claimed, so a non-empty read
        rules the mismatch out and stops there rather than repeating this
        method's own bug on the measured path. Without that read — the default, and
        what a connector CI leg always gets, since the runner has no route into
        the tenant vcluster — it hedges honestly and names the one check that
        settled FND-1284: the dispatched queue against the queue the owning
        app's worker prints in its own startup line.

        Args:
            node_name: The node whose queue to answer for.
            ae_result: The snapshot the line is being rendered from, for the
                child workflow id (``{ae_run_id}-{node_id}``).

        Returns:
            One sentence naming the cause when it was observed, else the honest
            either-or plus what to check first.
        """
        dispatch = self._node_dispatch.get(node_name)
        reading = (
            self._queue_pollers.get(dispatch.task_queue)
            if dispatch is not None and dispatch.task_queue
            else None
        )
        child = f"'{ae_result.run_id}-{node_name}'"
        if reading is None:
            return (
                "AE reports Running BOTH for a task no poller ever claimed and "
                "for one a worker claimed and stalled on, so this does not say "
                "which. Check the dispatched queue above against the queue the "
                "owning app's worker names in its own startup line (queue=...): a "
                "mismatch means nothing ever claimed it. If they match, read the "
                f"child workflow {child} on the tenant's Temporal — a history that "
                "stopped growing is a worker that took it and stopped making "
                "progress (or died holding it)."
            )
        if reading.unclaimed:
            return (
                "NO poller has claimed it: Temporal reports 0 pollers on "
                f"{reading.task_queue!r} in namespace {reading.namespace!r}, so "
                "nothing ever picked this task up — the deployed worker's queue "
                "does not match the dispatched one (or the worker is not running). "
                "The activity's own code is not implicated."
            )
        return (
            f"Something IS holding {reading.task_queue!r} in namespace "
            f"{reading.namespace!r}: Temporal reports {len(reading.pollers)} "
            f"poller(s), {reading.held_halves} ({reading.identities}). That rules "
            "out a queue-name mismatch, but a poller on the queue is not proof "
            "THIS task was claimed — the read answers who is holding the queue "
            f"now. Read the child workflow {child} on the tenant's Temporal: a "
            "history that stopped growing is a worker that took it and stopped "
            "making progress (or died holding it); a workflow task still pending "
            "means nothing has picked it up, which is what a worker polling only "
            "the other half of the queue looks like."
        )

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
            settled=lambda read: (
                read is not None and bool(read.dag) and _supersedes(read.version, seed)
            ),
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
            ae_result = await self._ae.poll_native_status(
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
            # Before the message is built, not after: this is what lets the
            # per-node line say whether anything claimed the task instead of
            # asserting one of the two causes. See _claim_clause.
            await self._capture_stop_point_pollers(stalled.result)
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
        # The ceiling path returns rather than raising, and its Running-node line
        # has the same question to answer as the watchdog's.
        await self._capture_stop_point_pollers(ae_result)
        return ae_result

    async def _capture_stop_point_pollers(self, ae_result: DAGRunResult) -> None:
        """Record who is holding the queues of the nodes still Running at the stop.

        Only the queues the diagnostic will actually ask about — one read per
        distinct queue among the nodes AE still reports ``Running`` — so a wide
        DAG does not turn a failure message into a fan of frontend calls, and a
        run that finished does no reads at all.

        Assigned wholesale, never mutated in place — this run's stop point is
        the only observation that may answer for it. A multi-DAG suite calls
        ``run_full_dag`` more than once on the same instance, so an accumulating
        dict would let the crawl's reading render as the verdict on the mine's
        node; and the class-level default is shared until ``setup_method``
        replaces it, so an in-place write would reach every other suite too.

        Args:
            ae_result: The snapshot the failure will be rendered from.
        """
        self._queue_pollers = {}
        if not ae_result.stopped_watching or not self._resolved_temporal_address():
            return
        queues = {
            dispatch.task_queue
            for node in ae_result.nodes
            if node.status is DAGNodeStatus.RUNNING
            and (dispatch := self._node_dispatch.get(node.name)) is not None
            and dispatch.task_queue
        }
        readings: dict[str, QueuePollerReading] = {}
        for queue in sorted(queues):
            reading = await self._read_queue_pollers(queue)
            if reading is not None:
                readings[queue] = reading
        self._queue_pollers = readings

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
        reading = await self._read_queue_pollers(self._extract_task_queue())
        if reading is None:
            return None
        if reading.unclaimed:
            return (
                f"Temporal confirms it: {reading.task_queue!r} in namespace "
                f"{reading.namespace!r} has NO pollers at all. Nothing is holding "
                "that queue, so the agent_spec().agent_name and the deployed "
                "worker's queue do not match (or the worker is not running)."
            )
        return (
            f"Temporal reports {len(reading.pollers)} poller(s) on "
            f"{reading.task_queue!r} in namespace {reading.namespace!r}, "
            f"{reading.held_halves}: {reading.identities}. Something IS holding "
            "that queue, so the node was not picked up for another reason — read "
            "the child workflow's history rather than hunting a queue-name "
            "mismatch."
        )

    async def _read_queue_pollers(self, queue: str) -> QueuePollerReading | None:
        """Ask Temporal who is polling *queue*, when that is possible at all.

        The one poller read in this class, with the queue as a parameter: the
        stall guard asks about the extract queue it inferred against, and the
        stuck-node diagnostic asks about whichever queue the seed DAG routed
        that node to — two questions, the same read, so a change to how the
        frontend is reached cannot land on one of them only.

        Args:
            queue: Task-queue name to describe.

        Returns:
            What Temporal answered, or ``None`` when no address is configured or
            the read itself failed. ``None`` is "not observed" and never "no
            pollers": the empty answer is a reading whose
            :attr:`QueuePollerReading.pollers` is empty, and conflating the two
            would let an unreachable frontend manufacture the exact finding this
            read exists to deliver.
        """
        address = self._resolved_temporal_address()
        if not address:
            return None
        namespace = self._resolved_temporal_namespace()
        try:
            from application_sdk.testing.harness.temporal import (  # noqa: PLC0415
                TemporalServiceReader,
                frontend_connection,
            )

            async def _connect() -> TemporalConnection:
                return await frontend_connection(address=address, namespace=namespace)

            async with TemporalServiceReader(connect=_connect) as reader:
                pollers = await reader.task_queue_pollers(queue, namespace=namespace)
        except Exception:
            logger.warning(
                "could not read Temporal at %s for task queue %s, so the "
                "diagnosis for that queue stays an inference",
                address,
                queue,
                exc_info=True,
            )
            return None
        return QueuePollerReading(
            task_queue=queue, namespace=namespace, pollers=tuple(pollers)
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
        attribute_reads: Mapping[str, AttributeSampleRead] | None = None,
        addressed_reads: Mapping[AssetRef, AttributeSampleRead] | None = None,
        edge_read: LineageEdgeRead | None = None,
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
            attribute_reads: Sampled attribute values as read.
            addressed_reads: Addressed assets' attribute values as read.
            edge_read: Lineage processes per declared edge as read, when the
                suite declared any.

        Returns:
            The outcome. Every reading is carried in both shapes: the settled
            projection a connector suite indexes, and the reading the assertion
            ladder grades.
        """
        counts = dict(count_reads or {})
        samples = dict(sample_reads or {})
        attributes = dict(attribute_reads or {})
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
                name: sampled_qualified_names(value)
                for name, value in samples.items()
                if not isinstance(value, Unreadable)
            },
            asset_attribute_samples={
                name: list(value)
                for name, value in attributes.items()
                if not isinstance(value, Unreadable)
            },
            asset_count_reads=counts,
            total_asset_read=total_read,
            asset_qn_reads=samples,
            asset_attribute_reads=attributes,
            addressed_attribute_reads=dict(addressed_reads or {}),
            lineage_edge_reads=edge_read,
            lineage_edge_counts=(
                {str(edge): count for edge, count in edge_read.counts.items()}
                if edge_read is not None and not isinstance(edge_read, Unreadable)
                else {}
            ),
            connection_expected=self._dag.expect_connection,
        )

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
                    "lineage_edge_counts": dict(outcome.lineage_edge_counts),
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
