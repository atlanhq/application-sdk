"""Setup and tenant plumbing layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

Per-test setup (it resolves the DAG runs and assigns every instance attribute
the layers above read), run identity, tenant auth, clients, timing budgets,
worker health and AE submission. Part of the linear mixin chain described in
:mod:`application_sdk.testing.e2e._base_config` (FND-4267).
"""

from __future__ import annotations

import os
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    AmbiguousDAGRunError,
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    ProgressWatchdogUnreachableError,
    WorkerNotHealthyError,
)
from application_sdk.testing.e2e.client import AEWorkflowClient
from application_sdk.testing.e2e.payload import (
    RunMode,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.e2e.tenant_pool import check_tenant_pool
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness._errors import MissingTenantEnvError
from application_sdk.testing.harness.automation_engine import (
    AEClient,
    cold_start_submit_kwargs,
)
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.identity import (
    Minter,
    TenantAuth,
    read_tenant_auth,
)
from application_sdk.testing.harness.outcome import (
    Settled,
)
from application_sdk.testing.harness.preconditions import (
    HealthReading,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.starters import (
    SubmitRetry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    from pyatlan.client.aio.client import AsyncAtlanClient


from application_sdk.testing.e2e._base_config import _E2EConfig
from application_sdk.testing.e2e._base_types import (
    _ATLAS_NOT_FOUND_ATTEMPTS,
    DAGSpec,
    ResolvedDAG,
    _derive_progress_stall_seconds,
    _normalise_lineage_edges,
)

logger = get_logger(__name__)


class _E2ETenantMixin(_E2EConfig):
    """Setup, identity, auth, clients, budgets, worker health and submission."""

    async def _setup_method_async(self) -> None:
        """Validate the declaration, mint this run's identity, open the clients.

        Raises:
            MissingHarnessClassAttrError: A required class attribute is unset.
            ProgressWatchdogUnreachableError: The pinned progress-stall window is
                not strictly below the poll ceiling, so the watchdog could never
                fire.
            MissingHarnessEnvError: The environment carries no tenant.
            TenantPoolMismatchError: The suite is not written for the tenant
                pool this run is on.
        """
        for required in type(self)._required_class_attrs:
            if not getattr(type(self), required, ""):
                raise MissingHarnessClassAttrError(
                    message=f"{type(self).__name__}: class attribute '{required}' must be set",
                    field=required,
                )
        check_tenant_pool(
            type(self)._tenant_pool, os.environ, suite=type(self).__name__
        )

        self._node_dispatch = {}
        self._queue_pollers = {}
        self._expected_node_identities = {}
        self._seed_version = None
        self._admin_reading = None
        # No run is active until run_full_dag activates one, so everything
        # resolves to the class's own DAG — which is the whole of what a
        # single-DAG suite ever sees.
        self._active_dag = None
        self.dag_outcomes = []
        # Whether the seed *completed* — the create landed AND the connection
        # became searchable. This is the idempotency flag ``seed_connection``
        # reads to reuse rather than re-create, and it is deliberately NOT what
        # teardown gates on: it lands after the searchability poll, so a create
        # that committed and then failed to become searchable would read as
        # "nothing exists here". The two flags below are the existence ones.
        self._connection_seeded = False
        # Whether anything has been sent that can bring this run's own
        # connection into being: the create write, and a DAG submit — recorded
        # as the connection that submit was about, because a submit against a
        # different one creates nothing here. Both are set on the way *in* to
        # the call, for the reason :meth:`_own_connection_may_exist` gives.
        self._connection_create_attempted = False
        self._submitted_connection_qns: list[str] = []
        self._payload_substitutions: MustacheSubstitutions | None = None
        self._dag_submitted = False
        # AE run ids submitted but not yet seen terminal. Teardown terminates
        # whatever is left — see :meth:`_terminate_unsettled_runs`.
        self._unsettled_run_ids: list[str] = []
        self._seeded_connection_qns: list[str] = []
        # Each seed's object-store root, keyed to the connection it was seeded
        # under — the QN whose connection-delete run is what reclaims it.
        self._seeded_prefixes: dict[str, str] = {}
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

    def _seed_readback_budget(self) -> Budget:
        """Allowance for a seed's published assets to become countable.

        The connection poll's timings rather than the inventory poll's: this
        waits for Elasticsearch to index a batch that was written moments ago,
        which is minutes-scale for a large tree, not the seconds the run's own
        inventory needs. The wait exits as soon as the expected count is
        reached, so a small seed pays nothing for the wider ceiling.

        Returns:
            The budget.
        """
        return Budget(
            timeout=timedelta(seconds=self.atlas_poll_timeout_seconds),
            poll_interval=timedelta(seconds=self.atlas_asset_poll_interval_seconds),
            # The probe returns a reading rather than raising, and a count that
            # is still climbing is the expected answer here — spending a
            # transient streak on it would end the wait early on exactly the
            # large seed the wider ceiling exists for.
            heartbeat=None,
        )

    def _publish_task_queue(self) -> str:
        """Task queue the tenant's ``publish`` app polls.

        Derived from :meth:`resolved_tenant_deployment_name` rather than pinned,
        for the reason that method exists: which deployment the system apps are
        registered under is a property of the tenant, and one suite runs against
        several tenants in one CI run.
        """
        return f"atlan-publish-{self.resolved_tenant_deployment_name()}"

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
            expected_asset_attributes={
                type_name: dict(attributes)
                for type_name, attributes in (
                    self.expected_asset_attributes
                    if spec.expected_asset_attributes is None
                    else spec.expected_asset_attributes
                ).items()
            },
            expected_asset_attributes_at={
                type_name: {
                    suffix: dict(attributes) for suffix, attributes in by_suffix.items()
                }
                for type_name, by_suffix in (
                    self.expected_asset_attributes_at
                    if spec.expected_asset_attributes_at is None
                    else spec.expected_asset_attributes_at
                ).items()
            },
            expected_lineage_edges=_normalise_lineage_edges(
                self.expected_lineage_edges
                if spec.expected_lineage_edges is None
                else spec.expected_lineage_edges
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

    def _track_unsettled_run(self, run_id: str) -> None:
        """Record an AE run id teardown must terminate if it never settles.

        Called the instant a run id becomes known — on a normal submit AND on
        the ambiguous-submit recovery path, which is why it is a sink
        :meth:`_submit` hands to ``submit_workflow`` rather than an append the
        caller does once the submit returns: AE submit is non-idempotent, so a
        submit that starts a run and then raises/times out must still leave the
        id tracked. Idempotent — the recovery hook and the normal 2xx return can
        both fire for one submit.

        Only :meth:`run_full_dag` submissions are tracked. The connection-delete
        and seed-publish DAGs are tenant-served quick runs that settle on their
        own, so they are intentionally not recorded here.
        """
        unsettled = self.__dict__.setdefault("_unsettled_run_ids", [])
        if run_id not in unsettled:
            unsettled.append(run_id)

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
            return await self._ae.submit_workflow(
                payload, slug=slug, on_run_id=self._track_unsettled_run
            )
        return await self._ae.submit_workflow(
            payload,
            slug=slug,
            retries=retry.retries,
            retry_sleep_seconds=retry.sleep_seconds,
            on_run_id=self._track_unsettled_run,
        )

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
