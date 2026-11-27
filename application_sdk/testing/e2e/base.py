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
  ephemeral connection name — including the qualified name teardown deletes,
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
* :mod:`~application_sdk.testing.harness.teardown` reclaims — through the
  tenant's ``connection-delete`` app, which owns the byte-stores a runner
  cannot reach, with the ``pyatlan`` purge as its fallback;
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

# FND-4267 moved BaseE2ETest's methods into the ``_base_*`` mixin layers, and
# with them most uses of the names imported below. They all stay imported: this
# module has no ``__all__``, so each one is importable surface that shipped
# (docs/standards/symbols.md). The F401 suppressions mark the ones it no longer
# uses itself.
import os  # noqa: F401
import re  # noqa: F401
import warnings  # noqa: F401
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence  # noqa: F401
from contextlib import AbstractAsyncContextManager, contextmanager  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import timedelta  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar  # noqa: F401

import httpx  # noqa: F401
import orjson
import pytest
from typing_extensions import deprecated

from application_sdk.common.task_queue import (  # noqa: F401
    QUEUE_PREFIX,
    application_name_from_env,
    deployment_name_from_env,
    derive_task_queue,
)
from application_sdk.contracts.types import ConnectionRef  # noqa: F401
from application_sdk.errors.base import safe_traceback, sanitize_cause_repr  # noqa: F401
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.binding import create_store_from_binding_optional  # noqa: F401
from application_sdk.testing._mustache import apply_mustache_subs

# Split out of this module (FND-4267); re-exported so every name it defined
# still imports from here.
from application_sdk.testing.e2e._base_assets import _E2EAssetsMixin
from application_sdk.testing.e2e._base_types import (  # noqa: F401
    _ATLAS_NOT_FOUND_ATTEMPTS,
    _CONNECTION_SELECTOR_SUBSTITUTION,
    _DEFAULT_SEED_COMPONENTS_DIR,
    _DEFAULT_SEED_STORE_BINDING,
    _EDGE_TYPE_NAME,
    _FINDING_TEMPLATES,
    _HARD_FAIL_NODE_STATUSES,
    _PROGRESS_STALL_CEILING_DIVISOR,
    _PROGRESS_STALL_MAX_SECONDS,
    _PROGRESS_STALL_MIN_SECONDS,
    DATABASE_SPEC_CREDENTIAL_TYPE_REMOVAL_VERSION,
    DAGSpec,
    FullDAGOutcome,
    NodeDispatch,
    QueuePollerReading,
    ResolvedDAG,
    _derive_progress_stall_seconds,
    _normalise_lineage_edges,
    _parse_lineage_edge,
    _render_finding,
    _SeedProbeReading,
    _supersedes,
)
from application_sdk.testing.e2e._errors import (  # noqa: F401
    AmbiguousDAGRunError,
    AtlasReadIndeterminateError,
    DAGProgressStalledError,
    DeployedManifestMismatchError,
    HarnessMethodNotImplementedError,
    InvalidLineageEdgeDeclarationError,
    ManifestDagMissingError,
    ManifestFileNotFoundError,
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    NoWorkerOnTaskQueueError,
    ProgressWatchdogUnreachableError,
    SeededConnectionNotSearchableError,
    WorkerNotHealthyError,
)
from application_sdk.testing.e2e._manifest_identity import (  # noqa: F401
    DagNodeIdentity,
    compare_node_identities,
    node_identities,
)
from application_sdk.testing.e2e.client import AEWorkflowClient
from application_sdk.testing.e2e.credential import CredentialBody  # noqa: F401
from application_sdk.testing.e2e.payload import (  # noqa: F401
    AgentSpec,
    ConnectionSpec,
    RunMode,
    build_ae_payload,
)
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions  # noqa: F401
from application_sdk.testing.e2e.tenant_pool import TenantPool, check_tenant_pool  # noqa: F401
from application_sdk.testing.harness import atlas  # noqa: F401
from application_sdk.testing.harness import seed as harness_seed  # noqa: F401
from application_sdk.testing.harness._errors import MissingTenantEnvError  # noqa: F401
from application_sdk.testing.harness.automation_engine import (  # noqa: F401
    AEClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    PublishedVersion,
    cold_start_submit_kwargs,
)
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.budgets import Budget  # noqa: F401
from application_sdk.testing.harness.evidence import (  # noqa: F401
    EvidenceBundle,
    secrets_from_environment,
    write_bundle,
)
from application_sdk.testing.harness.expectations import (  # noqa: F401
    UNREADABLE,
    AssetAttributes,
    AssetExpectations,
    AssetRef,
    AttributeExpectationValue,
    AttributeSampleRead,
    CountRead,
    Finding,
    LineageEdge,
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
from application_sdk.testing.harness.identity import (  # noqa: F401
    Minter,
    TenantAuth,
    read_tenant_auth,
)
from application_sdk.testing.harness.outcome import (  # noqa: F401
    Indeterminate,
    Outcome,
    Settled,
    as_attribute_samples,
    as_count,
    as_counts,
    as_lineage_edges,
    as_samples,
)
from application_sdk.testing.harness.preconditions import (  # noqa: F401
    HealthReading,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.starters import (  # noqa: F401
    AEWorkflowSpec,
    SubmitRetry,
    publish_seed_version,
)
from application_sdk.testing.harness.teardown import (  # noqa: F401
    CONNECTION_DELETE_NODE_ID,
    ConnectionDeletePlan,
    ConnectionDeleteReport,
    DeleteType,
    connection_delete_task_queue,
    delete_connection,
    purge_connection,
)
from application_sdk.testing.harness.waiting import poll_until  # noqa: F401

logger = get_logger(__name__)


@deprecated(
    "application_sdk.testing.e2e.base.delete_object is deprecated, does nothing, "
    "and will be removed in v4.0. Use application_sdk.storage.ops.delete instead."
)
async def delete_object(
    key: str,
    store: object = None,
    *,
    normalize: bool = True,
) -> bool:
    """Deprecated no-op kept only so the name stays importable until v4.0.

    .. deprecated:: 3.46
        Removed in v4.0. This name was ``base``'s import of
        :func:`application_sdk.storage.ops.delete` for the runner-side
        seed-object delete, which FND-3572 retired. No consumer used it, so
        rather than keep a working alias that new code could pick up, it now
        deletes nothing — call :func:`application_sdk.storage.ops.delete`.

    Args:
        key: Ignored.
        store: Ignored.
        normalize: Ignored.

    Returns:
        Always ``False``: nothing was deleted.
    """
    del key, store, normalize
    return False


class BaseE2ETest(_E2EAssetsMixin):
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
        """Reclaim this run's connections, then close the clients it opened.

        The reclaim itself is
        :func:`~application_sdk.testing.harness.teardown.delete_connection`,
        which submits a one-node ``connection-delete`` DAG the same way
        :meth:`seed_assets` submits its publish node — because the app owns the
        artifacts and runs *on the tenant*, where the byte-stores the harness
        cannot reach through the s3proxy are ordinary object-store keys. Since
        FND-3569 that includes each seed root under
        ``artifacts/apps/<app>/e2e-seed/``; the harness only checks the app's
        own report that it did.

        Nothing here raises. Every step reports, and the module those two
        functions live in explains why that is a stronger guarantee than
        remembering to wrap the call.

        Args:
            method: The test method pytest just ran. Unused; part of the xunit
                signature.
        """
        try:
            # First: a run still extracting must not race the delete of the
            # connection it is writing into.
            await self._terminate_unsettled_runs()
            await self._delete_connections()
        finally:
            await self._close_clients()

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
        return apply_mustache_subs(obj, subs)

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
        # Cleared first so a previous run's substitutions can never stand in
        # for this one's, under a ``_build_ae_payload`` override that does not
        # record its own.
        self._payload_substitutions = None
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
        # Set before the submit, not after: a submit that times out is a run
        # executing orphaned rather than one that never happened, and that run
        # creates the connection teardown has to reclaim. See
        # :meth:`_own_connection_may_exist`.
        if self._submit_may_create_connection():
            self._submitted_connection_qns = [
                *getattr(self, "_submitted_connection_qns", ()),
                self.connection_qualified_name,
            ]
        self._dag_submitted = True
        # The run id is recorded via _track_unsettled_run the instant _submit
        # knows it — on a normal submit AND on the ambiguous-submit recovery
        # path, from inside _submit rather than here. AE submit is
        # non-idempotent: a submit that starts a run but then raises/times out
        # must not leave an untracked orphan, so tracking cannot wait for this
        # call to return. Cleared only once AE reports the run terminal; every
        # exit in between — the watchdog, the ceiling, a no-worker verdict, a
        # failed manifest check — leaves a run that teardown has to stop.
        run_id = await self._submit(payload, slug=slug)
        logger.info("AE submit returned run_id=%s", run_id)
        # Idempotent fallback for an overridden _submit that does not wire the
        # sink: the returned id is tracked either way.
        self._track_unsettled_run(run_id)
        unsettled = self._unsettled_run_ids

        # Before the poll: submit is what makes Heracles fetch the tenant pod's
        # manifest and publish it over our seed, so this is the earliest point
        # the executed graph is knowable — and still seconds in, long before any
        # assertion could pass against the wrong app.
        await self._assert_deployed_manifest_matches(slug)

        ae_result = await self._poll_dag(run_id)
        if ae_result.status.is_terminal and run_id in unsettled:
            unsettled.remove(run_id)

        # Log-only, outcome-neutral. Placed after the poll so Elasticsearch
        # indexing lag cannot masquerade as an absence. See
        # probe_run_is_listed — this is what settles FND-676's gate.
        await self._ae.probe_run_is_listed(slug, run_id)

        return await self._read_atlas(ae_result)

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
          5. Asset attribute values: sampled assets satisfy the matchers
             declared in ``expected_asset_attributes`` (opt-in) — the only
             assertion about what an asset *contains* rather than how many
             landed or where.
          6. At least one Process/ColumnProcess exists (unless ``expect_lineage``
             is False).
          7. Each edge declared in ``expected_lineage_edges`` (opt-in) is linked
             by at least its floor of lineage processes.

        Assertions 2-6 are all about published inventory, so ``expect_connection
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
