"""Teardown layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

Reclaiming what a run created: unsettled AE runs, the run's own and seeded
connections (through the tenant's connection-delete app, with the pyatlan purge
as its fallback) and seeded object-store prefixes. Part of the linear mixin
chain described in :mod:`application_sdk.testing.e2e._base_config` (FND-4267).
"""

from __future__ import annotations

import asyncio

from application_sdk.errors.base import sanitize_cause_repr
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._base_tenant import _E2ETenantMixin
from application_sdk.testing.harness.teardown import (
    CONNECTION_DELETE_NODE_ID,
    ConnectionDeletePlan,
    ConnectionDeleteReport,
    connection_delete_task_queue,
    delete_connection,
    purge_connection,
)

logger = get_logger(__name__)

#: How long teardown waits for the AE pool to close. ``AEClient.aclose`` awaits
#: ``httpx.AsyncClient.aclose`` with no bound of its own, so a stalled transport
#: would otherwise hold teardown, and the test worker with it, indefinitely.
_AE_CLOSE_TIMEOUT_SECONDS = 30.0


class _E2ETeardownMixin(_E2ETenantMixin):
    """Reclaiming unsettled runs, connections and seeded prefixes."""

    async def _terminate_unsettled_runs(self) -> None:
        """Terminate every AE run this test submitted and never saw finish.

        The harness stops watching a run on several exits that leave it going —
        the progress watchdog, the poll ceiling, a no-worker verdict, or an
        extract child that AE is retrying after a failed first attempt. The
        extract node of a CI e2e run is dispatched to a task queue served only
        by the worker the CI job started, so once the job exits nothing can
        progress it, and the child waits on that queue until its workflow
        execution timeout (up to days) closes it as "Timed out". Those orphans
        are what a fleet-wide "Timed out" pattern on CI tenants is made of.

        Terminating the AE root closes the child with it (children are started
        with terminate-on-parent-close). Nothing here raises: an unstoppable run
        is logged, and the connection delete still runs.
        """
        run_ids = tuple(getattr(self, "_unsettled_run_ids", ()))
        ae = getattr(self, "_ae", None)
        if not run_ids or ae is None:
            return
        for run_id in run_ids:
            try:
                await ae.terminate_run(run_id)
            # conformance: ignore[E004] teardown boundary — a cleanup failure must never replace the run's verdict, and one unstoppable run must not skip the rest of teardown
            except Exception:
                logger.warning(
                    "e2e cleanup: could not terminate AE run %s; it may keep "
                    "running until its workflow execution timeout",
                    run_id,
                    exc_info=True,
                )
        self._unsettled_run_ids = []

    async def _delete_connections(self) -> None:
        """Delete every ephemeral connection this run touched, if there are any.

        The run's own connection first, then every lineage-parent connection
        :meth:`seed_assets` registered — in that order because the run's assets
        hold lineage *references* into the seeded skeletons, and deleting the
        referrer before the referent is the direction that cannot strand an
        edge. That ordering is also why each connection gets its own single-node
        run rather than all of them sharing one graph: chained ``depends_on``
        nodes would keep the order and let one stuck delete orphan every later
        one, parallel nodes would keep them independent and lose the order, and
        one run per connection keeps both.

        A delete that does not complete falls back to
        :func:`~application_sdk.testing.harness.teardown.purge_connection`,
        which reclaims the Atlas half from the runner and nothing else. The
        fallback is transitional — see the teardown module's docstring for the
        two silences it covers (a tier with no AE client, and a scale-to-zero
        worker that does not wake) and for when it should go.
        """
        conn_qn = getattr(self, "connection_qualified_name", "")
        if conn_qn and not self._own_connection_may_exist():
            logger.info(
                "e2e cleanup: no cleanup needed for %s — this run seeded no "
                "connection under that name and submitted no DAG that could "
                "create it, so nothing was ever created there",
                conn_qn,
            )
            # Emptied rather than filtered out of the loop below, so the
            # ordinals a seeding suite's teardown workflow names are built from
            # do not shift when the run's own connection is skipped.
            conn_qn = ""
        seeded = tuple(getattr(self, "_seeded_connection_qns", ()))
        # A suite that seeds under its own QN registers that name twice — once
        # as the run's own, once as seeded. One delete reclaims it; a second is
        # a full AE run against a connection the first already purged.
        # Skipped in place, for the same ordinal-stability reason as above.
        reached: set[str] = set()
        for ordinal, target in enumerate((conn_qn, *seeded), start=1):
            if not target or target in reached:
                continue
            reached.add(target)
            report = await self._delete_connection_via_app(target, ordinal=ordinal)
            if report is not None and report.complete:
                await self._confirm_seed_roots_purged(target, report)
                continue
            self._warn_connection_delete_incomplete(target, report)
            self._warn_seed_roots_left(target, report)
            await self._purge_connection_from_runner(target)

    def _own_connection_may_exist(self) -> bool:
        """Whether anything this run did could have created its own connection.

        ``setup_method`` mints ``connection_qualified_name`` before the test
        body runs, so by teardown it is always non-empty — non-emptiness is
        therefore no evidence that a connection exists under it. Two calls
        create one, and nothing else does:

        * the Atlas create :meth:`seed_connection` issues;
        * a DAG submit against that name, whose run mints the Connection on
          the tenant. A submit against a *different* connection — a
          :class:`DAGSpec` that rebinds the run, or a utility DAG pointed at a
          seeded connection (see :meth:`_submit_may_create_connection`) — is
          recorded under that other name and does not count here.

        A run that made neither call holds a freshly minted name under which,
        by construction, nothing can exist — a suite that skipped in
        :meth:`seed_prerequisites`, a leg where the source-availability tier
        wired no AE client at all, a test that errored before it submitted, or
        a connection-delete suite whose only DAG purged a seeded connection.
        Deleting that name costs an AE workflow publish, a submit and a
        minute of polling to reclaim nothing, on every leg of every push
        (FND-1873, FND-3405).

        **Both flags are set on the way in to the call, not on its way out**,
        and each for the same reason: a call whose response never arrived is
        not a call that did not happen. A submit that times out is a run
        executing orphaned, and an Atlas create whose reply is lost may still
        have committed. Recording success instead would turn each of those into
        a connection nobody reclaims on a shared tenant. Over-deleting is the
        harmless direction: the delete path already tolerates a connection that
        is not there.

        ``_connection_seeded`` is deliberately not consulted. It is
        :meth:`seed_connection`'s *idempotency* flag and lands only after the
        searchability poll, so a seed whose create committed and whose poll
        then timed out (:class:`SeededConnectionNotSearchableError`) reads as
        unseeded — which is precisely the half-set-up left-over teardown exists
        for.

        The one shape this cannot see is a suite that creates its connection
        by hand rather than through :meth:`seed_connection`. Seed through the
        hook and teardown follows; that is why the hook exists.

        Returns:
            Whether teardown has anything to reclaim under
            ``self.connection_qualified_name``.
        """
        if getattr(self, "_connection_create_attempted", False):
            return True
        own_qn = getattr(self, "connection_qualified_name", "")
        return own_qn in getattr(self, "_submitted_connection_qns", ())

    async def _delete_connection_via_app(
        self, qualified_name: str, *, ordinal: int
    ) -> ConnectionDeleteReport | None:
        """Run the ``connection-delete`` node for one connection.

        Args:
            qualified_name: The connection to delete.
            ordinal: Its position in this run's teardown, which is what keeps
                two teardown runs' AE workflow names apart — see
                :meth:`_connection_delete_plan`.

        Returns:
            The report, or ``None`` when this tier has no AE client to submit
            through at all. ``None`` is a distinct answer from a failed report:
            there is no missing app to name and nothing to link to, only the
            fallback.
        """
        ae = getattr(self, "_ae", None)
        if ae is None:
            # The worker-up-only tier (source_available=False) wires no AE
            # client. A run there still mints a connection name, and a suite
            # that created one by other means still needs it reclaimed.
            return None
        try:
            plan = self._connection_delete_plan(ordinal=ordinal)
        # conformance: ignore[E004] teardown boundary — resolving the plan reads the suite's own class attrs and can raise on a suite that never finished setup; a cleanup failure must never replace the run's verdict
        except Exception as error:
            # Reported as a failed delete rather than as "no AE client": the
            # client is there, and saying otherwise would send a reader looking
            # at the wrong tier. Logged here for the traceback, which the
            # report's redacted one-liner cannot carry.
            logger.warning(
                "e2e cleanup: could not resolve how to address the "
                "connection-delete app for %s",
                qualified_name,
                exc_info=True,
            )
            return ConnectionDeleteReport(
                qualified_name=qualified_name,
                errors=(
                    "could not resolve how to address the connection-delete "
                    f"app: {sanitize_cause_repr(error)}",
                ),
            )
        return await delete_connection(qualified_name, ae=ae, plan=plan)

    def _warn_connection_delete_incomplete(
        self, qualified_name: str, report: ConnectionDeleteReport | None
    ) -> None:
        """Say what the app did not clean up, and — when it can — why.

        A missing ``connection-delete`` app is called out by name rather than
        folded into a generic cleanup warning, because it is the failure mode
        that otherwise reads exactly like a passing run: the leg is green, the
        assets are gone (the fallback took them), and the byte-stores quietly
        accumulate on a shared tenant forever. A *superseded* DAG
        (:attr:`~application_sdk.testing.harness.teardown.ConnectionDeleteReport.dag_superseded`)
        gets the same treatment for a sharper reason: it reads like a tenant
        problem while being an SDK one, and since FND-1775 it should not be
        reachable at all. It stays a warning all the same —
        tenant cleanliness is not what the test is asserting, and a cleanup
        failure must never become the run's verdict.

        Args:
            qualified_name: The connection whose delete did not complete.
            report: What the delete managed, or ``None`` when no AE client
                existed to run it.
        """
        if report is None:
            logger.warning(
                "e2e cleanup: no AE client on this tier, so %s could not be "
                "deleted through the connection-delete app. Falling back to the "
                "runner-side purge, which reclaims the Atlas assets and leaves "
                "the connection's byte-stores (connection-cache/%s.sqlite and "
                "persistent-artifacts/apps/atlan-publish-app/state/%s/) behind",
                qualified_name,
                qualified_name,
                qualified_name,
            )
            return
        if report.app_absent:
            logger.warning(
                "e2e cleanup: nothing polled %s, so %s could not be deleted "
                "through the connection-delete app — either its scale-to-zero "
                "worker did not wake (keda.minReplicaCount is 0) or the app is "
                "not installed on this tenant (Global Marketplace app_id "
                "019ef7f4-de9a-77c3-bad1-7f201fc97052). Falling back to the "
                "runner-side purge, which reclaims the Atlas assets and leaves "
                "connection-cache/%s.sqlite and "
                "persistent-artifacts/apps/atlan-publish-app/state/%s/ behind — "
                "publish never cleans up its own cache, so those grow without "
                "bound on a shared tenant. Details: %s",
                self._connection_delete_task_queue(),
                qualified_name,
                qualified_name,
                qualified_name,
                "; ".join(report.errors),
            )
            return
        if report.dag_superseded:
            logger.warning(
                "e2e cleanup: %s was not deleted through the connection-delete "
                "app because the graph AE ran was not the delete node the "
                "teardown published (slug=%s run_id=%s). The teardown submits "
                "its own published version straight to AE, which fetches no "
                "manifest, so something republished over that version — an "
                "SDK-side or AE-side problem, not a tenant one, and one that "
                "should be impossible. Until it is understood this leg falls "
                "back to the runner-side purge and leaks "
                "connection-cache/%s.sqlite and "
                "persistent-artifacts/apps/atlan-publish-app/state/%s/. "
                "Details: %s",
                qualified_name,
                report.ae_workflow_slug or "<none>",
                report.ae_run_id or "<none>",
                qualified_name,
                qualified_name,
                "; ".join(report.errors),
            )
            return
        logger.warning(
            "e2e cleanup: the connection-delete run for %s did not complete "
            "(slug=%s run_id=%s), so its byte-stores may remain. Falling back to "
            "the runner-side purge. Details: %s",
            qualified_name,
            report.ae_workflow_slug or "<none>",
            report.ae_run_id or "<none>",
            "; ".join(report.errors),
        )

    async def _purge_connection_from_runner(self, qualified_name: str) -> None:
        """Reclaim the Atlas half of one connection with ``pyatlan``.

        The degraded path, run only when the app could not. Independently
        guarded, for the same reason the two phases inside
        :func:`~application_sdk.testing.harness.teardown.purge_connection` are:
        one connection that will not purge must not orphan the others.

        Args:
            qualified_name: The connection to purge.
        """
        try:
            async with self._atlas_client() as client:
                await purge_connection(client, qualified_name)
        # conformance: ignore[E004] teardown boundary — this runs after the assertions have decided the verdict, so a cleanup failure must never replace a real one; it is logged at WARNING with exc_info
        except Exception:
            logger.warning(
                "e2e cleanup: could not reach the tenant to purge %s — manual "
                "purge may be needed",
                qualified_name,
                exc_info=True,
            )

    def _seed_roots_for(self, qualified_name: str) -> list[str]:
        """The seed roots registered under one connection, in seed order.

        Args:
            qualified_name: The connection a teardown delete targeted.

        Returns:
            Every root :meth:`seed_assets` registered for it — usually none or
            one.
        """
        return [
            root
            for root, seeded_qn in getattr(self, "_seeded_prefixes", {}).items()
            if seeded_qn == qualified_name
        ]

    async def _confirm_seed_roots_purged(
        self, qualified_name: str, report: ConnectionDeleteReport
    ) -> None:
        """Check the delete's own report that it took this connection's seed roots.

        The harness cannot delete a seed root itself: the tenant's s3proxy
        refuses DELETE under ``/artifacts/apps/`` from a runner in every request
        shape (FND-1766's three-cloud A/B;
        :func:`~application_sdk.testing.harness.seed.seed_object_keys` has the
        detail). ``connection-delete`` runs on the tenant with no proxy in
        front of the bucket, and since FND-3569 its PURGE deletes
        ``artifacts/apps/<app>/e2e-seed/<encoded qn>/`` for every app and
        reports what it deleted on its output's ``storage_deleted_by_prefix``:
        non-zero counts only, keyed by prefix with a trailing ``/``.

        So a missing key is the signal. The harness wrote at least
        :func:`~application_sdk.testing.harness.seed.seed_object_keys` under
        every root, so a purge that reached one counts at least one object. A
        report with no ``storage_deleted_by_prefix`` at all is a tenant whose
        ``connection-delete`` predates FND-3569. Either way the warning names
        the root and the run: what an operator needs to clean it up and to see
        why it was left.

        Never raises, on the teardown rule.

        Args:
            qualified_name: The connection the completed delete targeted.
            report: That delete's report. ``complete`` is the caller's check.
        """
        roots = self._seed_roots_for(qualified_name)
        if not roots:
            return
        named_roots = ", ".join(f"{root}/" for root in roots)
        run_id = report.ae_run_id or "<none>"
        try:
            outputs = await self._ae.get_node_outputs(
                report.ae_run_id, CONNECTION_DELETE_NODE_ID
            )
        # conformance: ignore[E004] teardown boundary — see _purge_connection_from_runner; an unreadable report leaves the question open, which is strictly less harmful than replacing the run's verdict
        except Exception:
            logger.warning(
                "e2e cleanup: could not read the connection-delete run's "
                "outputs for %s (run_id=%s), so whether it purged the seed "
                "root(s) %s is unknown — manual cleanup may be needed",
                qualified_name,
                run_id,
                named_roots,
                exc_info=True,
            )
            return
        by_prefix = outputs.get("storage_deleted_by_prefix")
        if not isinstance(by_prefix, dict):
            logger.warning(
                "e2e cleanup: the connection-delete run for %s (run_id=%s) "
                "reported no storage_deleted_by_prefix, so this tenant's "
                "connection-delete predates FND-3569 and left the seed root(s) "
                "%s behind — manual cleanup may be needed until the app is "
                "upgraded",
                qualified_name,
                run_id,
                named_roots,
            )
            return
        for root in roots:
            deleted = by_prefix.get(f"{root}/")
            if deleted:
                logger.info(
                    "e2e cleanup: connection-delete purged %s object(s) under "
                    "the seed root %s/ (run_id=%s)",
                    deleted,
                    root,
                    run_id,
                )
                continue
            logger.warning(
                "e2e cleanup: the connection-delete run for %s (run_id=%s, "
                "delete_type=%s) did not report purging the seed root %s/, so "
                "it may still be there — manual cleanup may be needed. "
                "Prefixes it reported: %s",
                qualified_name,
                run_id,
                self.connection_delete_type.value,
                root,
                ", ".join(sorted(by_prefix)) or "<none>",
            )

    def _warn_seed_roots_left(
        self, qualified_name: str, report: ConnectionDeleteReport | None
    ) -> None:
        """Name the seed roots a delete that did not complete left behind.

        The runner-side fallback reclaims the Atlas half only, so a seed root
        under a connection whose ``connection-delete`` run did not complete is
        still there, and nothing else on the tenant will take it.

        Args:
            qualified_name: The connection whose delete did not complete.
            report: That delete's report, or ``None`` on a tier with no AE
                client.
        """
        roots = self._seed_roots_for(qualified_name)
        if not roots:
            return
        logger.warning(
            "e2e cleanup: the seed root(s) %s for %s were left behind — only "
            "connection-delete can reach them, and its run did not complete "
            "(run_id=%s). Manual cleanup may be needed",
            ", ".join(f"{root}/" for root in roots),
            qualified_name,
            (report.ae_run_id if report is not None else "") or "<none>",
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
            async with asyncio.timeout(_AE_CLOSE_TIMEOUT_SECONDS):
                await ae.aclose()
        # conformance: ignore[E004] teardown boundary — the same rule as the purge above; a pool that will not close (or not in time) must not become the test's verdict
        except Exception:
            logger.warning(
                "e2e cleanup: the AE connection pool did not close cleanly",
                exc_info=True,
            )

    def _connection_delete_task_queue(self) -> str:
        """Task queue the tenant's ``connection-delete`` app polls.

        Derived from :meth:`resolved_tenant_deployment_name` on the same rule as
        :meth:`_publish_task_queue`: which deployment the tenant registers its
        apps under is a property of the tenant, and one suite runs against
        several tenants in one CI run. Resolves to
        ``atlan-connection-delete-production`` on a standard tenant, mirroring
        ``atlan-publish-production``.

        Returns:
            The queue name.
        """
        return connection_delete_task_queue(self.resolved_tenant_deployment_name())

    def _connection_delete_plan(self, *, ordinal: int) -> ConnectionDeletePlan:
        """Resolve how this leg dispatches and waits on one connection's delete.

        Every value is the one this suite's own run uses, so teardown cannot be
        dispatched to a different tenant than the run it is cleaning up after.
        The envelope names the delete app
        (:mod:`application_sdk.testing.harness.teardown._dag`), so whichever
        graph wins Heracles' submit-time republish is a delete; ``app_service_url``
        stays omitted because a teardown submit has no service URL to name. Of
        what the plan does carry, two values are deliberately not the run's:

        * **The budgets** are teardown's own
          (:attr:`connection_delete_poll_timeout_seconds`,
          :attr:`connection_delete_stall_grace_seconds`) rather than the run's.
          Teardown is post-verdict, so a leg that sits in cleanup is holding a
          CI runner for nothing — and the stall grace here is the app-absent
          detector, which has to be short for exactly that reason.
        * **The AE workflow name** has to be unique twice over, because
          ``create_workflow`` is idempotent on the name: against the suite's own
          run (hence ``-teardown-``) and against the other connections this same
          teardown deletes (hence *ordinal*). It is the same collision
          :meth:`_seed_publish_plan` solves, for the same reason, and *ordinal*
          is used there too rather than a rendering of the QN: a QN's segments
          are caller-supplied, so any flattening of them into a name is a second
          encoding that can collide. The QN is not lost — it is on the
          workflow's description.

        Args:
            ordinal: This connection's position in the run's teardown, counting
                from one.

        Returns:
            The plan.
        """
        return ConnectionDeletePlan(
            connector_short_name=self.connector_short_name,
            task_queue=self._connection_delete_task_queue(),
            ae_workflow_name=(
                f"{self.connector_short_name}-{self.connection_name_prefix}-"
                f"{self.run_id}-teardown-{ordinal}"
            ),
            run_id=self.run_id,
            delete_type=self.connection_delete_type,
            # No cold-start budget. Since FND-1775 the teardown submits straight
            # to AE and calls no app pod, so there is nothing to cold-start-wait
            # on — whether the delete app's worker is up is asked by
            # ``stall_grace_seconds`` below, and leaving ``submit_retry`` unset
            # takes ``submit_published_version``'s publish-replication budget,
            # which is the only wait this submit actually has. Same reasoning as
            # ``_seed_publish_plan``.
            poll_interval_seconds=self.ae_poll_interval_seconds,
            poll_timeout_seconds=self.connection_delete_poll_timeout_seconds,
            stall_grace_seconds=self.connection_delete_stall_grace_seconds,
            minter=getattr(self, "_minter", None),
        )
