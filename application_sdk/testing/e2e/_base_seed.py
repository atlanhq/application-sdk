"""Seeding and spec layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

The prerequisite seeds (connection, assets, object store) and the agent and
connection specs the AE payload is built from. Part of the linear mixin chain
described in :mod:`application_sdk.testing.e2e._base_config` (FND-4267).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from application_sdk.common.task_queue import (
    QUEUE_PREFIX,
    application_name_from_env,
    deployment_name_from_env,
    derive_task_queue,
)
from application_sdk.contracts.types import ConnectionRef
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.binding import create_store_from_binding_optional
from application_sdk.testing.e2e._errors import (
    HarnessMethodNotImplementedError,
    SeededConnectionNotSearchableError,
)
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.payload import (
    AgentSpec,
    ConnectionSpec,
    RunMode,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness import seed as harness_seed
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.outcome import (
    Settled,
)
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    from obstore.store import ObjectStore


from application_sdk.testing.e2e._base_teardown import _E2ETeardownMixin
from application_sdk.testing.e2e._base_types import (
    _DEFAULT_SEED_COMPONENTS_DIR,
    _DEFAULT_SEED_STORE_BINDING,
    _SeedProbeReading,
)

logger = get_logger(__name__)

# Version that drops the deprecated ``DatabaseSpec.connector_config_name``
# fallback in :meth:`BaseE2ETest.resolved_connector_config_name`. Every
# deprecation in this SDK names its removal version; this is the one for that
# field. Defined in the module that formats the notice, so the
# deprecation checks (B002) can read the version off it.
DATABASE_SPEC_CREDENTIAL_TYPE_REMOVAL_VERSION = "4.0"


class _E2ESeedMixin(_E2ETeardownMixin):
    """Prerequisite seeding and the agent and connection specs."""

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
            # Before the write, not after: a create whose reply never arrived
            # may still have committed, and teardown must reclaim it. See
            # :meth:`_own_connection_may_exist`.
            self._connection_create_attempted = True
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
            except Exception as error:  # noqa: BLE001 — the probe is a suite-supplied write and can raise anything; narrowing it would let one connector's exception type escape the retry the loop exists to perform
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
            SeedPublishEmptyError: The publish run succeeded and Atlas holds
                nothing under the seeded connection. Publish is handed a
                *prefix*, and one it cannot read is an empty batch rather than
                an error — so this is the shape a node-status check cannot see,
                and the read-back is what turns it into a failure here instead
                of the connector's ``ATLAS-404`` cascade minutes later.
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
        # ``is None``, not ``or``. ``SeedSpec`` documents ``None`` as the one
        # omission sentinel, so ``""`` is a *supplied* value — and one
        # ``validate_resolved_spec`` rejects. Falling back on it would mint a
        # valid QN over the caller's mistake, and the seed would then publish
        # under a connection nobody asked for while the refs still named the
        # empty one. The empty-as-omitted collapse is the shape this spec
        # dropped its ``""`` sentinels to avoid.
        resolved = spec.resolve(
            qualified_name=(
                identity.qualified_name
                if spec.qualified_name is None
                else spec.qualified_name
            ),
            display_name=(
                identity.display_name
                if spec.display_name is None
                else spec.display_name
            ),
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

        # What keeps two seeds' AE workflow names apart — see
        # :meth:`_seed_publish_plan`. It is the teardown registry's length, so
        # it counts every connection this run touched (a ``DAGSpec``-named one
        # included), not only the seeds. That is deliberate and sufficient: the
        # registry is append-only within a run, so the value is strictly
        # increasing and no two seeds can ever read the same one.
        plan = self._seed_publish_plan(
            resolved, ordinal=len(self._seeded_connection_qns) + 1
        )
        self._seeded_connection_qns.append(resolved.qualified_name)
        root = harness_seed.seed_prefix_root(
            app_name=plan.app_name, qualified_name=resolved.qualified_name
        )
        self._seeded_prefixes[root] = resolved.qualified_name
        return await harness_seed.seed_assets(
            resolved,
            store=self.seed_object_store(),
            ae=self._ae,
            plan=plan,
            verify=self._count_seeded_assets,
        )

    async def _count_seeded_assets(
        self, qualified_name: str, expected: int
    ) -> int | None:
        """Poll Atlas until *expected* assets are visible under the seeded connection.

        The read-back that turns "the publish node succeeded" into "the seed
        landed". Two things it must not do, and both were wrong when this waited
        on the run's own short inventory budget:

        * **Give up in seconds.** The trees this exists for run to thousands of
          entities (the coalesce seed is ~130), and Elasticsearch does not index
          them instantly. ``atlas_asset_poll_timeout_seconds`` is 15s because a
          *crawl's* assets are already indexed by the time the DAG reports
          success; a seed's are not. This uses
          :meth:`_seed_readback_budget` instead.
        * **Report "ran out of time" as "found zero".** The wait's verdict and
          the last reading are different facts. Returning the reading alone let
          an expired poll whose last count was 0 raise ``SeedPublishEmptyError``
          — failing a large seed for being slow, as the unreadable-prefix
          failure it is not.

        Waiting for the full count rather than for "more than zero" is what
        makes the returned number worth grading: a partial answer is reported as
        partial rather than rounded up to success.

        Args:
            qualified_name: The seeded connection's QN.
            expected: How many assets the seed published; the poll exits as soon
                as the count reaches it.

        Returns:
            The last count read, or ``None`` when no probe ever produced a
            readable one — which the caller grades as unverified, never as zero.
        """
        last: int | None = None

        async def _probe() -> int | None:
            nonlocal last
            async with self._atlas_client() as client:
                reading = await atlas.count_total_assets(client, qualified_name)
            # An unreadable search leaves ``last`` at its previous value rather
            # than overwriting it with None: one blip late in the wait must not
            # erase a count the earlier probes did read.
            if isinstance(reading, Settled):
                last = reading.value
            return last

        await poll_until(
            _probe,
            settled=lambda count: count is not None and count >= expected,
            budget=self._seed_readback_budget(),
            label=f"{expected} seeded asset(s) under {qualified_name}",
        )
        return last

    def _seed_publish_plan(
        self, spec: harness_seed.ResolvedSeedSpec, *, ordinal: int
    ) -> harness_seed.SeedPublishPlan:
        """Resolve how this leg dispatches and waits on a seed's publish run.

        Every value is the one this suite's own run uses, so a seed cannot be
        polled on a different budget or dispatched to a different tenant than the
        run it exists to unblock. The AE workflow name is the exception, and it
        has to be unique twice over, because ``create_workflow`` is idempotent on
        the name:

        * against **the suite's own run** — sharing that name would publish the
          seed's one-node graph over the connector's DAG. Hence ``-seed-``.
        * against **another seed in the same run** — a suite that seeds two
          connections of the same type (two warehouses, two accounts) would
          otherwise reuse one slug, publish each graph over the other, and leave
          an AE run list in which the two are indistinguishable. This is the
          collision :meth:`_ae_workflow_name_suffix` already had to solve for
          multi-``DAGSpec`` runs.

        *ordinal* rather than a rendering of the QN: AE's own constraints on
        workflow names are not ours to guess, and a QN's segments are
        caller-supplied, so any flattening of them into a name is a second
        encoding that can collide (``a/b`` and ``a-b`` both read as ``a-b``).
        The position in the run is unique by construction and needs no escaping.
        The QN is not lost — it is on the workflow's description, and on the
        ``SeededConnection`` the call returns.

        Args:
            spec: The resolved spec, whose connector type names the workflow.
            ordinal: A number unique to this seed within the run. The caller
                takes it from the teardown registry's length, which counts every
                connection the run touched rather than only its seeds — the
                values are not contiguous, and do not need to be.

        Returns:
            The plan.
        """
        return harness_seed.SeedPublishPlan(
            app_name=self.connector_short_name,
            publish_task_queue=self._publish_task_queue(),
            ae_workflow_name=(
                f"{self.connector_short_name}-{self.connection_name_prefix}-"
                f"{self.run_id}-seed-{ordinal}-{spec.connector_type}"
            ),
            run_id=self.run_id,
            # No cold-start budget and no app address. Since FND-1766 the seed
            # submits straight to AE and calls no app pod, so there is nothing
            # to cold-start-wait on — the suite's own submit still carries
            # ``_submit_retry()``, which is where waiting on the app under test
            # belongs. Leaving ``submit_retry`` unset takes
            # ``submit_published_version``'s publish-replication budget, which
            # is the only wait this submit actually has.
            poll_interval_seconds=self.ae_poll_interval_seconds,
            poll_timeout_seconds=self.ae_poll_timeout_seconds,
            progress_stall_seconds=self._resolved_progress_stall_seconds(),
            minter=self._minter,
        )

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
