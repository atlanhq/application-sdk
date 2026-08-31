"""The two read-only cluster Protocols.

Split out of the package ``__init__`` alongside :mod:`._states` when the typed
backend landed (FND-241), for the same reason: the backend imports these to
declare what it satisfies, and a package ``__init__`` that both defines them and
imports the backend is a cycle. Both names are re-exported from
:mod:`application_sdk.testing.harness.cluster`, which stays the import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from application_sdk.testing.harness.cluster._states import (
    DeploymentState,
    HttpRequest,
    HttpResponse,
    LogLine,
    PodState,
    ResourceRef,
    ServiceTarget,
)

__all__ = ["ClusterReader", "CustomResourceReader"]


@runtime_checkable
class ClusterReader(Protocol):
    """Read built-in Kubernetes state. No mutation, by decision.

    Every method is ``async`` (decision D1). A backend over a synchronous client
    offloads it — that is the backend's problem, not the caller's, and
    :class:`~application_sdk.testing.harness.cluster.KubernetesReader` is the one
    that pays it.
    """

    async def deployments(
        self, namespace: str, selector: str
    ) -> Sequence[DeploymentState]:
        """Return the Deployments matching *selector* in *namespace*.

        Args:
            namespace: Namespace to read.
            selector: Label selector, in ``kubectl -l`` syntax.

        Returns:
            One :class:`DeploymentState` per match, possibly empty.

        Raises:
            Exception: If the read failed. An unreadable cluster is never an
                empty result — that fail-open shape is what FND-224's C4 is
                about, and a bounded wait turns the raise into
                :class:`~application_sdk.testing.harness.outcome.Indeterminate`.
        """
        ...

    async def pods(self, namespace: str, selector: str) -> Sequence[PodState]:
        """Return the pods matching *selector* in *namespace*.

        Args:
            namespace: Namespace to read.
            selector: Label selector, in ``kubectl -l`` syntax.

        Returns:
            One :class:`PodState` per match, possibly empty.

        Raises:
            Exception: If the read failed, for the reason :meth:`deployments`
                gives.
        """
        ...

    def logs(
        self, namespace: str, selector: str, *, since: timedelta | None = None
    ) -> AsyncIterator[LogLine]:
        """Stream container output from the pods matching *selector*.

        Not ``async def``: an async generator's caller wants ``async for``, and
        declaring this ``async def`` would make the call return a coroutine that
        has to be awaited before there is an iterator to iterate.

        Args:
            namespace: Namespace to read.
            selector: Label selector, in ``kubectl -l`` syntax.
            since: Only lines newer than this. ``None`` means from the start of
                the container's retained output.

        Yields:
            One :class:`LogLine` per line, interleaved across matching pods.
        """
        ...

    async def http(self, target: ServiceTarget, request: HttpRequest) -> HttpResponse:
        """Make an HTTP call against an in-cluster Service.

        How the call reaches the Service is the backend's business — an ephemeral
        ``kubectl port-forward`` from outside, or a direct call from a driver
        that already sits inside the cluster.

        Args:
            target: Service and port to reach.
            request: The call to make.

        Returns:
            The response, whatever its status. A non-2xx is a value here, not an
            exception: the caller's predicate decides what counts as failure.
        """
        ...


@runtime_checkable
class CustomResourceReader(Protocol):
    """Read custom resources, parameterised by :class:`ResourceRef`.

    Separate from :class:`ClusterReader` so that Protocol never becomes the union
    of every consumer's needs.
    """

    async def custom_resources(
        self,
        ref: ResourceRef,
        *,
        namespace: str | None = None,
        selector: str | None = None,
        name: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return the custom resources of *ref*, narrowed by whatever is given.

        *namespace* is keyword-only and nullable, and *name* exists, because the
        two reads scenario one actually makes need them: a CRD's schema is
        cluster-scoped, and ``Scaling.from_twd()`` reads *one* named
        ``TemporalWorkerDeployment`` rather than filtering a list — a TWD carries
        no label that distinguishes it from its siblings, so a selector cannot
        express that read at all.

        Args:
            ref: API coordinates of the kind to read.
            namespace: Namespace to read, or ``None`` for a cluster-scoped kind.
            selector: Optional label selector.
            name: Read exactly this one resource instead of listing.

        Returns:
            The raw resource bodies. Untyped by design: the harness has no schema
            for another repo's CRDs, and inventing one here would be the same
            widening :class:`ResourceRef` avoids. This is the deliberate
            exception to the repo's typed-contract rule —
            :meth:`ClusterReader.deployments` and :meth:`ClusterReader.pods` stay
            typed, because those schemas are Kubernetes' own and this SDK can
            hold them.

            Empty when the kind is not installed on this cluster: that is a clean
            404 and a real answer.

        Raises:
            Exception: On any error that is *not* a clean 404 — see
                :meth:`crd_schema` for why the line is drawn exactly there.
        """
        ...

    async def crd_schema(self, ref: ResourceRef) -> Mapping[str, Any] | None:
        """Return the CRD's OpenAPI schema, or ``None`` when it is not installed.

        Args:
            ref: API coordinates of the kind to look up.

        Returns:
            The schema body, or ``None`` when the CRD is genuinely absent.

        Raises:
            Exception: On any error that is *not* a clean 404. A 403, an expired
                token or a timeout must raise rather than be cached as "not
                installed" — the same rule as
                :class:`~application_sdk.testing.harness.outcome.Indeterminate`:
                a read that failed is neither a pass nor a component fault.
        """
        ...
