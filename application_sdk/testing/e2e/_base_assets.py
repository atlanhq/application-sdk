"""Asset-grading layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

The Atlas reads a finished run is graded on and the assertions over them. Part
of the linear mixin chain described in
:mod:`application_sdk.testing.e2e._base_config` (FND-4267).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    AtlasReadIndeterminateError,
)
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.automation_engine import (
    DAGRunResult,
)
from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    AssetExpectations,
    AssetRef,
    AttributeSampleRead,
    CountRead,
    Finding,
    LineageEdgeRead,
    SampleRead,
    Unreadable,
    evaluate_attributes,
    evaluate_attributes_at,
    evaluate_counts,
    evaluate_lineage_edges,
    evaluate_locations,
    normalise_attribute_expectations,
    normalise_attribute_expectations_at,
    sampled_qualified_names,
)
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    Outcome,
    Settled,
    as_attribute_samples,
    as_count,
    as_counts,
    as_lineage_edges,
    as_samples,
)
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    from pyatlan.client.aio.client import AsyncAtlanClient


from application_sdk.testing.e2e._base_poll import _E2EPollMixin
from application_sdk.testing.e2e._base_types import (
    FullDAGOutcome,
    ResolvedDAG,
    _render_finding,
)

logger = get_logger(__name__)


class _E2EAssetsMixin(_E2EPollMixin):
    """Atlas reads and the asset assertions a run is graded on."""

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
        # NOTE: adding a location or attribute type here does NOT by itself make
        # the poll WAIT for that type — the loop only stays alive via a per-type
        # floor or the non-empty backstop (total == 0), so a sample-only type
        # with no floor can still be zero when the loop exits (the moment any
        # other type makes total > 0). The real wait-for-this-type safeguard is
        # pairing each expected_asset_qn_depth / expected_asset_attributes type
        # with an expected_min_asset_counts floor — see those attrs' docstrings.
        dag = self._dag
        probe_types = tuple(
            {
                *dag.expected_min_asset_counts,
                *dag.expected_exact_counts,
                *dag.expected_asset_qn_depth,
                *dag.expected_asset_attributes,
                *dag.expected_asset_attributes_at,
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
                await atlas.sample_located_names(
                    client,
                    self.connection_qualified_name,
                    tuple(dag.expected_asset_qn_depth),
                    ancestor_attributes=self.asset_qn_ancestor_attributes,
                ),
                tuple(dag.expected_asset_qn_depth),
            )
            logger.info(
                "Atlas qualifiedName samples under %s: %s",
                self.connection_qualified_name,
                {
                    name: value
                    if isinstance(value, Unreadable)
                    else sampled_qualified_names(value)
                    for name, value in sample_reads.items()
                },
            )

        # Sample attribute VALUES for the contents assertion (opt-in). Its own
        # search rather than a widening of the one above: the two knobs name
        # different type sets, and a suite that declares only depths must not
        # start paying for attribute includes it never asked for.
        attribute_reads: Mapping[str, AttributeSampleRead] = {}
        if dag.expected_asset_attributes:
            attribute_reads = as_attribute_samples(
                await atlas.sample_asset_attributes(
                    client,
                    self.connection_qualified_name,
                    {
                        type_name: tuple(attributes)
                        for type_name, attributes in dag.expected_asset_attributes.items()
                    },
                    per_type=self.asset_attribute_sample_size,
                ),
                tuple(dag.expected_asset_attributes),
            )
            logger.info(
                "Atlas attribute samples under %s: %s",
                self.connection_qualified_name,
                dict(attribute_reads),
            )

        # Read the individually addressed assets (opt-in). One search per ref,
        # which is why this is for the handful of assets a fixture pins rather
        # than a way to walk a type.
        addressed_reads: Mapping[AssetRef, AttributeSampleRead] = {}
        addressed = normalise_attribute_expectations_at(
            dag.expected_asset_attributes_at
        )
        if addressed:
            addressed_reads = as_attribute_samples(
                await atlas.read_asset_attributes(
                    client,
                    self.connection_qualified_name,
                    {ref: tuple(matchers) for ref, matchers in addressed.items()},
                ),
                tuple(addressed),
            )
            logger.info(
                "Atlas addressed-asset attributes under %s: %s",
                self.connection_qualified_name,
                {str(ref): value for ref, value in addressed_reads.items()},
            )

        # Count the declared lineage edges (opt-in). Separate from the lineage
        # presence read above: that one asks whether assets have lineage at
        # all, and stays true when one kind of link silently stops landing.
        edge_read: LineageEdgeRead | None = None
        if dag.expected_lineage_edges:
            edge_read = as_lineage_edges(
                await atlas.count_lineage_edges(
                    client,
                    self.connection_qualified_name,
                    list(dag.expected_lineage_edges),
                )
            )
            logger.info(
                "Lineage edges under %s: %s",
                self.connection_qualified_name,
                edge_read,
            )

        return self._outcome(
            ae_result,
            connection_in_atlas=True,
            connection_read=connection_read,
            count_reads=count_reads,
            total_read=total_read,
            lineage_read=lineage_read,
            sample_reads=sample_reads,
            attribute_reads=attribute_reads,
            addressed_reads=addressed_reads,
            edge_read=edge_read,
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
            # Coerced here, once, at the boundary between what a suite writes
            # (a bare scalar is sugar for ``Exactly``) and what the evaluator
            # grades (always a matcher).
            attributes=normalise_attribute_expectations(dag.expected_asset_attributes),
            attributes_at=normalise_attribute_expectations_at(
                dag.expected_asset_attributes_at
            ),
            require_nonempty=dag.require_nonempty_assets,
            # getattr, because the count half of this is a pure function of the
            # class attributes and is unit-tested on an instance that never ran
            # setup_method. An empty prefix makes the location half a no-op,
            # which is exactly what "there is no connection to measure depth
            # from" should do.
            connection_qualified_name=getattr(self, "connection_qualified_name", ""),
            lineage_edges=dict(dag.expected_lineage_edges),
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

    def _attribute_findings(
        self, samples: Mapping[str, AttributeSampleRead]
    ) -> Sequence[Finding]:
        """Grade the sampled attribute values against the declared matchers.

        Args:
            samples: Sampled assets and their attribute values, as read.

        Returns:
            One finding per (asset, attribute) whose value did not satisfy its
            matcher.
        """
        return evaluate_attributes(samples, self._asset_expectations())

    def _addressed_attribute_findings(
        self, reads: Mapping[AssetRef, AttributeSampleRead]
    ) -> Sequence[Finding]:
        """Grade the individually addressed assets against their matchers.

        Args:
            reads: Each addressed ref -> every asset whose qualifiedName matched
                its suffix, as read.

        Returns:
            One finding per unmet matcher, plus one per ref that matched no
            asset or more than one.
        """
        return evaluate_attributes_at(reads, self._asset_expectations())

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

    def _evaluate_lineage_edges(self, edge_reads: LineageEdgeRead | None) -> list[str]:
        """Evaluate ``expected_lineage_edges`` against the observed edge counts.

        For suites that crawl once in a fixture and assert piecemeal, which
        never reach the full assertion ladder. Pass the outcome's
        ``lineage_edge_reads``.

        Args:
            edge_reads: What the edge read answered, or ``None`` when none was
                taken (graded as zero processes).

        Returns:
            One failure line per declared edge below its floor; empty when every
            edge met it or none is declared.

        Raises:
            AtlasReadIndeterminateError: An edge could not be graded, because the
                read failed or the scan stopped at its cap below the floor. Never
                reported as a count of zero.
        """
        findings = evaluate_lineage_edges(edge_reads, self._asset_expectations())
        self._raise_if_ungraded(findings)
        return [_render_finding(finding) for finding in findings]

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

    def _validate_asset_attributes(
        self, asset_attribute_samples: Mapping[str, AttributeSampleRead]
    ) -> list[str]:
        """Validate sampled assets carry the declared attribute values.

        The sibling of :meth:`_evaluate_asset_expectations` and
        :meth:`_validate_asset_locations` for the third question — not *how
        many* assets landed, nor *where*, but *what they contain*. The logic is
        :func:`~application_sdk.testing.harness.expectations.evaluate_attributes`.

        The gap it closes: a connector that computes an attribute — a count, a
        size, a partition flag, ``lastSyncRunAt`` — can publish a structurally
        perfect tree in which the computed value is a degraded ``0``, or stops
        being set at all, and every count and depth assertion passes. Both are
        the same number to a count check and different states in Atlas, so this
        check keeps present-but-zero and absent apart all the way from the
        search payload into the failure line.

        Args:
            asset_attribute_samples: Sampled assets and their values, as read.

        Returns:
            Human-readable failure lines, empty when every sampled asset
            satisfied every matcher. Types with no sampled assets are skipped —
            "too few / none" is the COUNT check's job, which is why every type
            declared in ``expected_asset_attributes`` should carry a floor too.
        """
        return [
            _render_finding(finding)
            for finding in self._attribute_findings(asset_attribute_samples)
        ]

    def _validate_addressed_asset_attributes(
        self, addressed_attribute_reads: Mapping[AssetRef, AttributeSampleRead]
    ) -> list[str]:
        """Validate individually addressed assets carry the declared values.

        The per-asset half of the attribute check, and the one that can pin a
        value differing *within* a type — five schemas at five different view
        counts share no per-type claim about ``viewsCount`` beyond
        ``Present()``. The logic is
        :func:`~application_sdk.testing.harness.expectations.evaluate_attributes_at`.

        Args:
            addressed_attribute_reads: Each addressed ref -> every asset whose
                qualifiedName matched its suffix, as read.

        Returns:
            Human-readable failure lines, empty when every addressed asset
            resolved to exactly one match satisfying every matcher. Unlike the
            per-type check, a ref matching **nothing** is a failure line rather
            than a skip: the suite named the asset, so its absence is the claim.
        """
        return [
            _render_finding(finding)
            for finding in self._addressed_attribute_findings(addressed_attribute_reads)
        ]

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
            # Assertions 2-6 are all about published inventory, and the probes
            # that feed them never ran. Evaluating them against empty counts
            # would fail every run — the zero-asset backstop in
            # _evaluate_asset_expectations most obviously. The DAG gate above is
            # the verdict; anything further is the suite's own to assert.
            logger.info(
                "%s declares expect_connection=False — DAG succeeded; skipping the "
                "asset-count, asset-location, asset-attribute and lineage assertions",
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
        attribute_findings = [
            *self._attribute_findings(
                outcome.asset_attribute_reads or outcome.asset_attribute_samples
            ),
            *self._addressed_attribute_findings(outcome.addressed_attribute_reads),
        ]
        # Ungraded before unmet, always. A finding that exists because a search
        # could not be READ is not evidence about the connector, and reporting it
        # as one is what sent an Atlas outage to the connector team as "the
        # floors were not met". It is raised first, and as a leaf that is not an
        # AssertionError, so pytest marks the leg an error rather than a failure.
        edge_findings = evaluate_lineage_edges(
            outcome.lineage_edge_reads, self._asset_expectations()
        )
        self._raise_if_ungraded(
            [*count_findings, *location_findings, *attribute_findings, *edge_findings]
        )

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

        attribute_failures = [
            _render_finding(finding) for finding in attribute_findings
        ]
        if attribute_failures:
            raise AssertionError(
                "Published assets carry the wrong attribute values under "
                f"{outcome.connection_qualified_name} (the right number of "
                "assets landed in the right place, but what they contain is "
                "wrong — a computed value degraded, or an attribute stopped "
                "being set):\n" + "\n".join(attribute_failures)
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

        edge_failures = [_render_finding(finding) for finding in edge_findings]
        if edge_failures:
            raise AssertionError(
                "Declared lineage edges are missing under "
                f"{outcome.connection_qualified_name} (lineage may exist, but "
                "not between these types):\n"
                + "\n".join(edge_failures)
                + f"\nObserved per edge: {outcome.lineage_edge_counts}"
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
