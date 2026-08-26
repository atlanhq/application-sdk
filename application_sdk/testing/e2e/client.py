"""The synchronous face of the harness's Automation Engine and Atlas readers.

This module used to be 2,475 lines carrying two unrelated jobs on one class:
talking to the Automation Engine, and reading Atlas. Child F on FND-224 split
them into :mod:`application_sdk.testing.harness.automation_engine` and
:mod:`application_sdk.testing.harness.atlas`, both ``async`` throughout
(decision D1). What is left here is the seam between those and the code that
calls them.

:class:`AEWorkflowClient` keeps every method name, signature and return type it
had. Each one is now a one-liner over
:func:`~application_sdk.testing.harness.bridge.run_sync`, which is what turns
five ad-hoc ``asyncio.run`` calls — each standing up a fresh event loop, a fresh
``AsyncAtlanClient`` and therefore a fresh TLS handshake, up to ~50 times inside
a single 1,500-second Atlas poll — into one reused loop per thread. It also
closes a gap none of the five had: ``asyncio.run`` inside a running loop raises
a bare ``RuntimeError`` from deep in asyncio, while ``run_sync`` raises
:class:`~application_sdk.testing.harness._errors.SyncBridgeInAsyncContextError`
naming the ``_async`` twin to await instead.

**This shim is temporary by design.** Child H re-expresses ``testing/e2e`` over
the harness and moves the sync boundary up to ``BaseE2ETest``'s public methods,
at which point this class and the two adapters below go away. Two things are
therefore kept deliberately narrow so that deletion is a deletion and not a
rewrite:

* the fail-open collapse lives in exactly one function,
  :func:`_settled_or_fail_open`, rather than in four scattered ``except``
  blocks;
* nothing new is added to the surface. Callers that want the third answer —
  :class:`~application_sdk.testing.harness.outcome.Indeterminate`, "the search
  could not be read", as distinct from "it read zero" — call the harness
  functions directly.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.automation_engine import (
    AEClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
    PublishedVersion,
    RunLookup,
    WriteRecovery,
    cold_start_submit_kwargs,
)
from application_sdk.testing.harness.automation_engine.client import (
    _RECONCILE_INTERVAL_SECONDS,
    _RECONCILE_TIMEOUT_SECONDS,
)
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Outcome, Settled

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

__all__ = [
    "AEWorkflowClient",
    "DAGNodeResult",
    "DAGNodeStatus",
    "DAGRunResult",
    "DAGRunStatus",
    "PublishedVersion",
    "RunLookup",
    "WriteRecovery",
    "cold_start_submit_kwargs",
]

T = TypeVar("T")

#: Default types the per-type count and lineage reads cover, kept on this
#: module's signatures as the tuple they have always been.
_DEFAULT_TYPE_NAMES: tuple[str, ...] = atlas.DEFAULT_TYPE_NAMES


def _settled_or_fail_open(outcome: Outcome[T], fallback: T) -> T:
    """Unwrap a settled Atlas read, or fall back to today's empty value.

    **The one place the fail-open behaviour still exists**, and the whole of
    what child H deletes. The harness readers now distinguish "the search
    returned nothing" from "the search could not be read"; every caller of this
    class takes a ``dict[str, int]`` or a ``bool`` and grades it, so the third
    answer has nowhere to go until those call sites learn it. Collapsing here
    keeps ``testing/e2e``'s observable behaviour identical to what it was, and
    keeps the collapse countable — one function, one log line — rather than
    spread across four ``except Exception: return {}`` blocks.

    Args:
        outcome: What the harness reader returned.
        fallback: The value this method returned on a search error before the
            split — zeros, ``[]`` or ``False``.

    Returns:
        The settled value, or ``fallback`` for any other verdict.
    """
    if isinstance(outcome, Settled):
        return outcome.value
    logger.error(
        "%s could not be read (%s) — reporting %r, which is what this method "
        "returned on a search error before the harness split. The reader "
        "itself now says Indeterminate; only this sync adapter still collapses "
        "it, and a run graded on this value may be reporting an Atlas fault as "
        "a connector one",
        outcome.label,
        type(outcome).__name__,
        fallback,
    )
    return fallback


class AEWorkflowClient:
    """Thin sync wrapper over the harness's AE and Atlas readers.

    Stateless aside from the auth material and the pooled connections its two
    readers hold. Every method is idempotent and safe to retry except
    :meth:`submit_workflow`.

    Args:
        tenant_url: Base URL of the tenant (e.g. ``https://devex.atlan.com``).
            Trailing slash is stripped if present.
        api_token: Bearer token used for AE / Atlas REST calls. Accepts
            either a long-lived API key or a short-lived OAuth
            ``client_credentials`` access token.
        oauth_client_id: Optional OAuth client id, with its secret. When
            supplied, the ``AsyncAtlanClient`` used for asset search
            authenticates via OAuth ``client_credentials`` instead of the
            bearer api_token. This yields a *different* service-account
            identity than the API key — useful when the API key's service
            account isn't on an asset's admin ACL but the OAuth client is.
        oauth_client_secret: The secret for ``oauth_client_id``.
    """

    def __init__(
        self,
        tenant_url: str,
        api_token: str,
        *,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
    ) -> None:
        self.tenant_url = tenant_url.rstrip("/")
        self._api_token = api_token
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret
        self._ae = AEClient(self.tenant_url, api_token)

    def close(self) -> None:
        """Close the AE connection pool. Idempotent; a later call reopens one.

        Not required — an unclosed pool lives until the process exits, which is
        survivable in a test process — but a long-lived driver that builds many
        clients should call it. The Atlas reads open and close their own client
        per call, so there is nothing to close for that half.
        """
        run_sync(self._ae.aclose())

    def _atlas(self) -> AbstractAsyncContextManager[AsyncAtlanClient]:
        """Open an Atlas client with this harness's configured identity."""
        return atlas.atlas_client(
            self.tenant_url,
            self._api_token,
            oauth_client_id=self._oauth_client_id,
            oauth_client_secret=self._oauth_client_secret,
        )

    # ------------------------------------------------------------------
    # Automation Engine
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        name: str,
        description: str = "",
        *,
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        """Create or upsert an AE workflow; see :meth:`AEClient.create_workflow`."""
        return run_sync(
            self._ae.create_workflow(
                name,
                description,
                retries=retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
        )

    def wait_for_slug(self, slug: str) -> bool:
        """Wait until AE resolves *slug*; see :meth:`AEClient.wait_for_slug`.

        Advisory: the returned bool is for the caller's log, and an unresolved
        slug is not an error — ``create_version``'s own 404 retry is what makes
        the sequence safe. Replaces the unconditional ``time.sleep(3)`` both
        full-DAG harnesses ran here (FND-240).
        """
        return run_sync(self._ae.wait_for_slug(slug))

    def create_version(
        self,
        slug: str,
        version_payload: dict[str, Any],
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> int:
        """Create a workflow version; see :meth:`AEClient.create_version`."""
        return run_sync(
            self._ae.create_version(
                slug,
                version_payload,
                retries=retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
        )

    def publish_version(
        self,
        slug: str,
        version: int,
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> None:
        """Publish a workflow version; see :meth:`AEClient.publish_version`."""
        run_sync(
            self._ae.publish_version(
                slug,
                version,
                retries=retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
        )

    def get_published_version(self, slug: str) -> PublishedVersion | None:
        """Read the published version; see :meth:`AEClient.get_published_version`."""
        return run_sync(self._ae.get_published_version(slug))

    def find_run_created_since(
        self,
        slug: str,
        since: datetime,
        *,
        timeout_seconds: int = _RECONCILE_TIMEOUT_SECONDS,
        interval_seconds: int = _RECONCILE_INTERVAL_SECONDS,
    ) -> RunLookup:
        """Resolve an ambiguous submit; see :meth:`AEClient.find_run_created_since`."""
        return run_sync(
            self._ae.find_run_created_since(
                slug,
                since,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
            )
        )

    def probe_run_is_listed(self, slug: str, run_id: str) -> bool | None:
        """Log-only listing probe; see :meth:`AEClient.probe_run_is_listed`."""
        return run_sync(self._ae.probe_run_is_listed(slug, run_id))

    def submit_workflow(
        self,
        payload: dict[str, Any],
        *,
        slug: str = "",
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        """Submit a run; see :meth:`AEClient.submit_workflow`.

        The one non-idempotent write on this class. Every guard that keeps a
        retry from spawning a duplicate run lives in the async method.
        """
        return run_sync(
            self._ae.submit_workflow(
                payload,
                slug=slug,
                retries=retries,
                retry_sleep_seconds=retry_sleep_seconds,
            )
        )

    def get_native_status(self, run_id: str) -> DAGRunResult:
        """One ``native-status`` read; see :meth:`AEClient.get_native_status`."""
        return run_sync(self._ae.get_native_status(run_id))

    def poll_native_status(
        self,
        run_id: str,
        *,
        interval_seconds: int = 10,
        timeout_seconds: int = 600,
        max_transient_failures: int = 5,
        stall_grace_seconds: int | None = None,
        stall_task_queue: str = "",
        progress_stall_seconds: int | None = None,
    ) -> DAGRunResult:
        """Poll the DAG run; see :meth:`AEClient.poll_native_status`."""
        return run_sync(
            self._ae.poll_native_status(
                run_id,
                interval_seconds=interval_seconds,
                timeout_seconds=timeout_seconds,
                max_transient_failures=max_transient_failures,
                stall_grace_seconds=stall_grace_seconds,
                stall_task_queue=stall_task_queue,
                progress_stall_seconds=progress_stall_seconds,
            )
        )

    # ------------------------------------------------------------------
    # Atlas
    # ------------------------------------------------------------------

    def connection_exists_in_atlas_via_search(self, qualified_name: str) -> bool:
        """Search-based Connection probe; see
        :func:`~application_sdk.testing.harness.atlas.connection_exists`.

        Returns True iff at least one Connection asset matches the QN. A search
        that could not be read still returns ``False`` here — see
        :func:`_settled_or_fail_open`.
        """

        async def _read() -> bool:
            async with self._atlas() as client:
                return _settled_or_fail_open(
                    await atlas.connection_exists(client, qualified_name), False
                )

        return run_sync(_read())

    def poll_atlas_for_connection(
        self,
        qualified_name: str,
        *,
        interval_seconds: int = 30,
        timeout_seconds: int = 1500,
        max_forbidden_attempts: int = 5,
        max_not_found_attempts: int = 10,
        max_not_found_attempts_override: int | None = None,
    ) -> bool:
        """Poll Atlas until the Connection appears or the budget elapses.

        The whole poll now runs on **one** ``AsyncAtlanClient`` — the change
        FND-242 calls the main prize. Each iteration previously built a new
        event loop and a new client, so a 1,500-second poll at the 30-second
        default paid up to ~50 TLS handshakes to answer one boolean.

        ``max_not_found_attempts`` becomes the budget's two per-probe caps: the
        start grace (``(n - 1) * interval`` — every probe that reached the old
        check was an empty search, so the cap fired on attempt *n*) and the
        transient-failure streak. Splitting the one number that way keeps the
        total tolerance identical while separating the diagnoses, which is the
        point: an Atlas outage used to be reported as "the Connection never
        materialised", sending the reader to the connector.

        Args:
            qualified_name: The Connection's exact qualified name.
            interval_seconds: Gap between probes.
            timeout_seconds: Total budget. Wide by default (25 min) because
                publish runs after the AE DAG completes and can take a while to
                flush large connections. Callers with smaller datasets can
                tighten this.
            max_forbidden_attempts: Vestigial and unread. The poll goes through
                the search index, whose ACL is permissive, so a 403 never
                surfaces here. Kept on the signature for back-compat.
            max_not_found_attempts: Consecutive unproductive probes to tolerate.
            max_not_found_attempts_override: When set, replaces
                ``max_not_found_attempts``.

        Returns:
            True once the Connection is searchable; False on every other
            verdict, which is what this method returned before the split.
        """
        if max_not_found_attempts_override is not None:
            max_not_found_attempts = max_not_found_attempts_override
        del max_forbidden_attempts
        budget = Budget(
            timeout=timedelta(seconds=timeout_seconds),
            poll_interval=timedelta(seconds=interval_seconds),
            start_grace=timedelta(
                seconds=(max_not_found_attempts - 1) * interval_seconds
            ),
            max_transient_failures=max_not_found_attempts,
            # The reader logs its own per-probe line; a heartbeat would double it.
            heartbeat=None,
        )

        async def _poll() -> bool:
            async with self._atlas() as client:
                return _settled_or_fail_open(
                    await atlas.poll_for_connection(
                        client, qualified_name, budget=budget
                    ),
                    False,
                )

        return run_sync(_poll())

    def count_assets_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...] = _DEFAULT_TYPE_NAMES,
    ) -> dict[str, int]:
        """Per-typeName counts of active assets under a connection's QN prefix.

        See :func:`~application_sdk.testing.harness.atlas.count_assets`. Returns
        ``{typeName: count}`` with zeros for types that produced no matches —
        and, until child H, also zeros for a search that could not be read (see
        :func:`_settled_or_fail_open`).
        """

        if not type_names:
            return {}

        async def _read() -> dict[str, int]:
            async with self._atlas() as client:
                return dict(
                    _settled_or_fail_open(
                        await atlas.count_assets(
                            client, connection_qualified_name, type_names
                        ),
                        dict.fromkeys(type_names, 0),
                    )
                )

        return run_sync(_read())

    def count_total_assets_under_connection(
        self, connection_qualified_name: str
    ) -> int:
        """Total descendant-asset count under the connection prefix, ALL types.

        See :func:`~application_sdk.testing.harness.atlas.count_total_assets`.
        Returns 0 on an unreadable search, as it did before the split.
        """

        async def _read() -> int:
            async with self._atlas() as client:
                return _settled_or_fail_open(
                    await atlas.count_total_assets(client, connection_qualified_name),
                    0,
                )

        return run_sync(_read())

    def count_lineage_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...] = _DEFAULT_TYPE_NAMES,
    ) -> dict[str, int]:
        """Per-typeName count of entity assets with lineage attached.

        See :func:`~application_sdk.testing.harness.atlas.count_lineage`.
        """

        if not type_names:
            return {}

        async def _read() -> dict[str, int]:
            async with self._atlas() as client:
                return dict(
                    _settled_or_fail_open(
                        await atlas.count_lineage(
                            client, connection_qualified_name, type_names
                        ),
                        dict.fromkeys(type_names, 0),
                    )
                )

        return run_sync(_read())

    def sample_asset_qualified_names_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...],
        per_type: int = 3,
    ) -> dict[str, list[str]]:
        """Sample up to *per_type* qualifiedNames per type under the connection.

        See
        :func:`~application_sdk.testing.harness.atlas.sample_qualified_names`.
        Returns ``{typeName: [qualifiedName, ...]}`` with an empty list for
        types that produced no hits — and, until child H, for a search that
        could not be read.
        """

        if not type_names:
            return {}

        async def _read() -> dict[str, list[str]]:
            async with self._atlas() as client:
                sampled = _settled_or_fail_open(
                    await atlas.sample_qualified_names(
                        client,
                        connection_qualified_name,
                        type_names,
                        per_type=per_type,
                    ),
                    {name: [] for name in type_names},
                )
                return {name: list(qns) for name, qns in sampled.items()}

        return run_sync(_read())
