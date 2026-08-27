"""The typed Kubernetes backend behind the cluster Protocols.

Replaces the ``kubectl``-shelling reads in ``testing/e2e/{pods,logs}.py`` rather
than wrapping them (FND-241): those three functions —  ``get_pods``,
``get_pod_logs``, ``wait_for_pods_ready`` — are deleted in the same change. Two
backends for one job would have meant keeping the ``orjson``-parses-but-catches-
``json.JSONDecodeError`` shape and the ``return []`` alive to be maintained, and
neither had a caller outside this repo to protect.

**Read-only, and structurally so.** Only ``list``/``read``/``get`` verbs are
reached for. The runtime scenario suite's mutations — patching a TWD's timings,
suspending a Flux ``HelmRelease``, server-side-applying a chart — stay on the
suite side (agreed on FND-224, 2026-08-17), so nothing here can be repurposed
into a writer without adding a verb that is visibly not a read.

**Three things this backend does that the ``kubectl`` reads could not.**

*It cannot fail open.* Every failure raises — :class:`ClusterReadFailedError`
carrying the status and the context it went through. ``get_pods`` returned ``[]``
on a ``kubectl`` failure, which is how an unreadable cluster came to be graded as
an empty one. A bounded wait turns the raise into
:class:`~application_sdk.testing.harness.outcome.Indeterminate` through its
transient classifier, which is the C4 fix: "could not look" and "looked, saw
nothing" are different answers.

*Its 404 narrowing is exact.* On a custom-resource read a 404 means "this CRD is
not installed here" and is answered with an empty result; a 403 from a narrowed
role, an expired token or a timeout raises rather than being cached as a false
negative. On a built-in read even a 404 raises, because a missing namespace is a
setup fault and not an empty match. This is the runtime side's rule, adopted
verbatim — see :mod:`application_sdk.testing.harness.outcome` for why two
independent designs landing on it is the argument for it.

*Its field paths are the YAML's field paths.* Every object is passed through the
client's ``sanitize_for_serialization`` before it is read, so
``status.readyReplicas`` in this module is the same string a reader would use
against ``kubectl get -o yaml``. Reading the generated snake_case attributes
instead would make every field path in a gauge or an assertion a translation of
the one in the manifest.

**Synchronous client under an ``async`` Protocol.** ``kubernetes`` is sync, so
every call is offloaded with
:func:`~application_sdk._runtime.offload.run_in_thread`. Not
``asyncio.to_thread``: P031 walks whole files and exempts only
``_runtime/offload.py``, so a bare ``to_thread`` here is flagged whatever the
context — and the dedicated pool brings the labelling and hold instrumentation
for free. P031's actual hazard (starving a Temporal worker's shared pool) does
not arise, because the driver process runs pytest, not a worker.

**One ``ApiClient`` per thread.** The client is not thread-safe, and the offload
pool hands the same read to different threads over a run, so the bundle is built
in :class:`threading.local` — the donated design, kept. It also means the seam
that decides *where the credentials come from* is a factory, not a subclass:
:func:`kubeconfig_apis` is the out-of-cluster one, and FND-248's in-cluster
backend is another factory passed to the same reader rather than a second reader.
"""

from __future__ import annotations

import asyncio
import functools
import heapq
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import httpx

from application_sdk._runtime.offload import run_in_thread
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.cluster._errors import (
    ClusterReadFailedError,
    KubeconfigUnavailableError,
    KubernetesExtraMissingError,
)
from application_sdk.testing.harness.cluster._portforward import port_forward
from application_sdk.testing.harness.cluster._states import (
    DeploymentState,
    HttpRequest,
    HttpResponse,
    LogLine,
    PodPhase,
    PodState,
    ResourceRef,
    ServiceTarget,
)

logger = get_logger(__name__)

__all__ = [
    "AppsReads",
    "CoreReads",
    "CustomObjectReads",
    "CustomResourceDefinitionReads",
    "KubernetesApis",
    "KubernetesReader",
    "kubeconfig_apis",
]

#: Default per-call bound handed to the client as ``_request_timeout``. Distinct
#: from any enclosing wait's budget: one hung API call must not silently consume
#: the whole window a poll was given.
_DEFAULT_REQUEST_TIMEOUT = timedelta(seconds=30)

#: Reported phase string -> :class:`PodPhase`. Built once so an unrecognised
#: phase is a miss rather than a raised-and-caught ``ValueError``.
_PHASES = {phase.value: phase for phase in PodPhase}

#: How much container output a log read returns by default. Bounded because a
#: crash-looping worker's stream is unbounded and the interesting part is the
#: end; ``None`` asks for everything the container has retained.
_DEFAULT_TAIL_LINES = 10_000


# ---------------------------------------------------------------------------
# The client surface, as Protocols
# ---------------------------------------------------------------------------
#
# ``kubernetes`` ships no type information, so its API classes and every model
# they return are untyped. Naming the handful of methods this backend actually
# calls does two things a bare ``Any`` would not: it states the dependency
# surface (four methods on CoreV1Api, one on AppsV1Api, four on
# CustomObjectsApi, one on ApiextensionsV1Api — nothing else, and nothing that
# writes), and it gives the tests a double that is checked against that surface
# rather than a mock that accepts anything.
#
# The *return* annotations stay ``object``: these are the client's own model
# classes, and inventing a typed mirror of a dependency's models is the widening
# ``ResourceRef`` exists to avoid. Every one of them is converted to a dict by
# ``KubernetesApis.sanitize`` before a single field is read, so nothing
# downstream of that conversion is untyped.


class CoreReads(Protocol):
    """The read verbs this backend uses from ``CoreV1Api``."""

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> object:
        """List pods in a namespace."""
        ...

    def read_namespaced_pod_log(
        self, name: str, namespace: str, **kwargs: Any
    ) -> object:
        """Read one container's output.

        Annotated ``object`` rather than ``str`` because the client's generated
        signature returns a union covering its own ``_preload_content=False`` and
        ``async_req=True`` modes, neither of which this backend uses. The caller
        narrows it, so a client that ever returned something else is a caught
        surprise rather than a downstream ``AttributeError``.
        """
        ...


class AppsReads(Protocol):
    """The read verbs this backend uses from ``AppsV1Api``."""

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> object:
        """List Deployments in a namespace."""
        ...


class CustomObjectReads(Protocol):
    """The read verbs this backend uses from ``CustomObjectsApi``."""

    def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **kwargs: Any
    ) -> object:
        """List a namespaced custom resource."""
        ...

    def list_cluster_custom_object(
        self, group: str, version: str, plural: str, **kwargs: Any
    ) -> object:
        """List a cluster-scoped custom resource."""
        ...

    def get_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        **kwargs: Any,
    ) -> object:
        """Read one namespaced custom resource by name."""
        ...

    def get_cluster_custom_object(
        self, group: str, version: str, plural: str, name: str, **kwargs: Any
    ) -> object:
        """Read one cluster-scoped custom resource by name."""
        ...


class CustomResourceDefinitionReads(Protocol):
    """The read verb this backend uses from ``ApiextensionsV1Api``."""

    def read_custom_resource_definition(self, name: str, **kwargs: Any) -> object:
        """Read a CRD by its ``{plural}.{group}`` name."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class KubernetesApis:
    """One thread's API objects, plus the model-to-dict conversion they share.

    Built by a factory — :func:`kubeconfig_apis` today — so that *where the
    credentials come from* is a parameter of :class:`KubernetesReader` rather
    than a subclass of it.

    Attributes:
        core: ``CoreV1Api``, for pods and their logs.
        apps: ``AppsV1Api``, for Deployments.
        custom: ``CustomObjectsApi``, for custom resources.
        crds: ``ApiextensionsV1Api``, for CRD schemas.
        sanitize: The client's ``sanitize_for_serialization``. Carried on the
            bundle rather than reached for through a module import because it is
            an ``ApiClient`` method, and this backend holds one per thread.
        kube_context: Which context these clients were built for, for a failure
            to name. ``None`` when the factory did not choose one.
    """

    core: CoreReads
    apps: AppsReads
    custom: CustomObjectReads
    crds: CustomResourceDefinitionReads
    sanitize: Callable[[object], Any]
    kube_context: str | None = None


class _NoApiException(Exception):
    """Stands in for ``ApiException`` when ``kubernetes`` is not installed.

    Never raised and never matched by anything — an ``except`` against it simply
    catches nothing. That is the correct behaviour for a process running the
    reader against an injected test double: with no real client in play there are
    no API errors to narrow, and a narrowing that silently swallowed the double's
    exceptions instead would be worse than one that does nothing.
    """


@functools.cache
def _api_exception() -> type[BaseException]:
    """The client's ``ApiException``, or a never-matching stand-in.

    Cached because it is consulted on every read's failure path, and an import
    inside an ``except`` clause is the one place an import cost is paid while
    something is already going wrong.
    """
    try:
        from kubernetes.client.exceptions import ApiException  # noqa: PLC0415
    except ImportError:  # pragma: no cover — only when the extra is absent
        logger.debug(
            "The `harness` extra is not installed, so there is no ApiException "
            "to narrow against — every read error will propagate as itself",
            exc_info=True,
        )
        return _NoApiException
    return ApiException


def _status_of(error: BaseException) -> int | None:
    """HTTP status an API error carried, or ``None`` if it carried none."""
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else None


@contextmanager
def _reading(target: str, kube_context: str | None) -> Iterator[None]:
    """Convert an API error into a typed leaf; let anything else through.

    Only the client's own ``ApiException`` is converted. A ``TypeError`` from a
    wiring bug is a bug, and dressing it as ``DEPENDENCY_UNAVAILABLE`` would let
    a bounded wait absorb it as a transient blip and spend its whole budget on
    it — the fail-open shape, one level up.

    Args:
        target: What was being read, as a noun phrase for the report.
        kube_context: Context the read went through, or ``None``.
    """
    # ``except <expression>`` rather than ``except Exception`` + isinstance: the
    # client's error type is a lazy import (the extra is optional), and catching
    # it by name is what makes "anything that is not an API error propagates
    # unchanged" a property of the syntax rather than of a branch that can rot.
    try:
        yield
    except _api_exception() as error:
        status = _status_of(error)
        raise ClusterReadFailedError(
            message=(
                f"Could not read {target}"
                + (f" (HTTP {status})" if status is not None else "")
            ),
            target=target,
            status=status,
            kube_context=kube_context,
            cause=error,
        ) from error


def kubeconfig_apis(*, kube_context: str | None = None) -> KubernetesApis:
    """Build one thread's API bundle from the ambient kubeconfig.

    ``load_kube_config`` honours ``exec`` credential plugins, which is what keeps
    vcluster kubeconfigs working — and the one real advantage the ``kubectl``
    backend had, which is why it does not survive as a reason to keep it.

    The configuration is loaded into a *fresh* ``Configuration`` object rather
    than the client's global default. The global is process-wide, so loading into
    it from several offload-pool threads would have them overwriting each other's
    credentials mid-run.

    Args:
        kube_context: Context to use, or ``None`` for the kubeconfig's current
            one. Per-call timeouts are applied by :class:`KubernetesReader` at
            the call, not baked into the client, so they are not a parameter
            here.

    Returns:
        The bundle, usable from the calling thread only.

    Raises:
        KubernetesExtraMissingError: If the ``harness`` extra is not installed.
        KubeconfigUnavailableError: If there is no readable kubeconfig, or it has
            no such context.
    """
    try:
        from kubernetes import client, config  # noqa: PLC0415
    # conformance: ignore[E008] the miss is re-raised as KubernetesExtraMissingError naming the extra to install; logging it here as well would report the same gap twice
    except ImportError as error:
        raise KubernetesExtraMissingError(
            message=(
                "The typed Kubernetes cluster reader needs the `harness` extra: "
                "pip install 'atlan-application-sdk[harness]'"
            ),
            cause=error,
        ) from error

    configuration = client.Configuration()
    try:
        config.load_kube_config(
            context=kube_context, client_configuration=configuration
        )
    except Exception as error:
        raise KubeconfigUnavailableError(
            message=(
                "No usable kubeconfig"
                + (f" for context {kube_context!r}" if kube_context else "")
                + " — check KUBECONFIG, or log the vcluster in"
            ),
            kube_context=kube_context,
            cause=error,
        ) from error

    api_client = client.ApiClient(configuration=configuration)
    # The client ships no type information, so each of these is an untyped
    # callable being checked against the Protocols above. The casts are the one
    # place that gap is acknowledged; everything downstream is typed.
    return KubernetesApis(
        core=client.CoreV1Api(api_client),
        apps=client.AppsV1Api(api_client),
        custom=client.CustomObjectsApi(api_client),
        crds=client.ApiextensionsV1Api(api_client),
        sanitize=api_client.sanitize_for_serialization,
        kube_context=kube_context,
    )


class KubernetesReader:
    """Read cluster state through the in-process typed client.

    Satisfies both
    :class:`~application_sdk.testing.harness.cluster.ClusterReader` and
    :class:`~application_sdk.testing.harness.cluster.CustomResourceReader`; a
    consumer that needs only one asks for only one.

    Args:
        apis: Factory building one thread's API bundle. Defaults to
            :func:`kubeconfig_apis` against the ambient kubeconfig. This is the
            seam that keeps "where do the credentials come from" out of the
            reader: FND-248's in-cluster backend is another factory here, not a
            second reader class.
        kube_context: Kubeconfig context, passed to the default factory. Ignored
            when *apis* is supplied.
        request_timeout: Per-call bound on every API read.
        tail_lines: Default cap on how many lines a log read returns. ``None``
            asks for everything the container has retained.

    Example:
        >>> reader = KubernetesReader(kube_context="e2e-gcp")  # doctest: +SKIP
        >>> pods = await reader.pods("default", "app=worker")  # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        apis: Callable[[], KubernetesApis] | None = None,
        kube_context: str | None = None,
        request_timeout: timedelta = _DEFAULT_REQUEST_TIMEOUT,
        tail_lines: int | None = _DEFAULT_TAIL_LINES,
    ) -> None:
        self._kube_context = kube_context
        self._request_timeout = request_timeout
        self._tail_lines = tail_lines
        self._build = apis or functools.partial(
            kubeconfig_apis, kube_context=kube_context
        )
        self._local = threading.local()

    @property
    def kube_context(self) -> str | None:
        """Kubeconfig context these reads go through, or ``None`` for the current.

        Public because it is not merely informational: anything that reaches the
        same cluster by another route — ``LogCollector``'s ``kubectl describe``,
        the port-forward behind :meth:`http` — has to pin the *same* context, and
        a private attribute would leave each of them guessing.
        """
        return self._kube_context

    # -- the client, per thread ---------------------------------------------

    def _apis(self) -> KubernetesApis:
        """This thread's bundle, built on first use.

        Called from inside the offload pool, so "this thread" is whichever pool
        thread picked the call up. The client is not thread-safe; a bundle per
        thread is how that is honoured without serialising every read behind a
        lock.
        """
        existing: KubernetesApis | None = getattr(self._local, "apis", None)
        if existing is not None:
            return existing
        built = self._build()
        self._local.apis = built
        return built

    def _timeout_seconds(self) -> float:
        return self._request_timeout.total_seconds()

    # -- ClusterReader ------------------------------------------------------

    async def deployments(
        self, namespace: str, selector: str = ""
    ) -> Sequence[DeploymentState]:
        """Return the Deployments matching *selector* in *namespace*.

        Args:
            namespace: Namespace to read.
            selector: Label selector in ``kubectl -l`` syntax. Empty matches all.

        Returns:
            One :class:`DeploymentState` per match, in the API server's order.

        Raises:
            ClusterReadFailedError: If the read did not come back with data.
                Never an empty sequence for an unreadable cluster.
        """
        target = f"deployments in {namespace} matching {selector or '<all>'}"

        def _list() -> Sequence[Mapping[str, Any]]:
            apis = self._apis()
            with _reading(target, apis.kube_context):
                listing = apis.apps.list_namespaced_deployment(
                    namespace,
                    label_selector=selector,
                    _request_timeout=self._timeout_seconds(),
                )
            return _items(apis.sanitize(listing))

        return [
            _deployment_state(item, namespace) for item in await run_in_thread(_list)
        ]

    async def pods(self, namespace: str, selector: str = "") -> Sequence[PodState]:
        """Return the pods matching *selector* in *namespace*.

        Args:
            namespace: Namespace to read.
            selector: Label selector in ``kubectl -l`` syntax. Empty matches all.

        Returns:
            One :class:`PodState` per match, in the API server's order.

        Raises:
            ClusterReadFailedError: If the read did not come back with data.
        """
        target = f"pods in {namespace} matching {selector or '<all>'}"

        def _list() -> Sequence[Mapping[str, Any]]:
            apis = self._apis()
            with _reading(target, apis.kube_context):
                listing = apis.core.list_namespaced_pod(
                    namespace,
                    label_selector=selector,
                    _request_timeout=self._timeout_seconds(),
                )
            return _items(apis.sanitize(listing))

        return [_pod_state(item, namespace) for item in await run_in_thread(_list)]

    async def logs(
        self, namespace: str, selector: str = "", *, since: timedelta | None = None
    ) -> AsyncIterator[LogLine]:
        """Stream container output from the pods matching *selector*.

        Every container of every matching pod is read, and the lines are merged
        on their server-side timestamps — so a request that one pod handled and
        another logged about appears in the order it happened, which is the whole
        reason a selector-wide log read is more useful than a per-pod one.

        Not a live tail: each container's retained output is read once, bounded by
        this reader's ``tail_lines``. A wait that needs to *watch* for a line
        polls this inside
        :func:`~application_sdk.testing.harness.waiting.poll_until` — the budget
        and the stall watchdog belong to that primitive, not to a stream.

        Args:
            namespace: Namespace to read.
            selector: Label selector in ``kubectl -l`` syntax.
            since: Only lines newer than this. ``None`` reads from the start of
                the container's retained output.

        Yields:
            One :class:`LogLine` per line.

        Raises:
            ClusterReadFailedError: If the pod listing or a log read failed.
        """
        sources = [
            (pod.name, container)
            for pod in await self.pods(namespace, selector)
            for container in pod.containers or {}
        ]
        if not sources:
            return
        texts = await asyncio.gather(
            *(
                self.container_log(namespace, pod, container, since=since)
                for pod, container in sources
            )
        )
        streams = [
            _sortable(_log_lines(pod, container, text))
            for (pod, container), text in zip(sources, texts, strict=True)
        ]
        for _key, line in heapq.merge(*streams, key=lambda entry: entry[0]):
            yield line

    async def http(self, target: ServiceTarget, request: HttpRequest) -> HttpResponse:
        """Make an HTTP call against an in-cluster Service.

        Reached through a ``kubectl port-forward`` tunnel — the one place this
        backend still shells out, because a port-forward is transport rather than
        a read and ``kubernetes.stream``'s equivalent is a socket-level API, not
        a drop-in (see
        :mod:`application_sdk.testing.harness.cluster._portforward`).

        The tunnel is pinned to this reader's :attr:`kube_context`. Without that
        it would follow whichever context the kubeconfig marks current, and a
        reader built for one cluster would read pods from it while tunnelling
        into another — both calls succeeding, nothing logged, and the only
        symptom a result that makes no sense.

        Args:
            target: Service and port to reach.
            request: The call to make.

        Returns:
            The response, whatever its status. A non-2xx is a value here, not an
            exception: the caller's predicate decides what counts as failure.
        """
        timeout_seconds = request.timeout.total_seconds()
        async with port_forward(
            target.namespace,
            target.service,
            target.port,
            timeout=timeout_seconds,
            kube_context=self._kube_context,
        ) as session:
            response = await session.request(
                request.method,
                request.path,
                body=request.body,
                headers=request.headers,
            )
        return HttpResponse(
            status=response.status_code,
            body=_json_or_none(response),
            text=response.text,
        )

    # -- CustomResourceReader ----------------------------------------------

    async def custom_resources(
        self,
        ref: ResourceRef,
        *,
        namespace: str | None = None,
        selector: str | None = None,
        name: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return the custom resources of *ref*, narrowed by whatever is given.

        Four reads behind one signature, chosen by which arguments are present:
        namespaced or cluster-scoped by *namespace*, one-by-name or a list by
        *name*. Parameterised rather than one method per kind for the reason
        :class:`ResourceRef` is: every consumer's new kind would otherwise be a
        change in this repo.

        Args:
            ref: API coordinates of the kind to read.
            namespace: Namespace to read, or ``None`` for a cluster-scoped kind.
            selector: Optional label selector. Ignored when *name* is given —
                a named read is already a single object.
            name: Read exactly this one resource instead of listing.

        Returns:
            The raw resource bodies, camelCase as in their YAML. Empty when the
            CRD is not installed on this cluster, or when a named resource does
            not exist: both are clean 404s, and both are readable answers.

            Untyped by design: the harness has no schema for another repo's CRDs.
            The stated exception to the typed-contract rule — ``deployments()``
            and ``pods()`` stay typed because those schemas are Kubernetes' own.

        Raises:
            ClusterReadFailedError: On anything that is not a clean 404. A 403
                from a narrowed role, an expired token or a timeout must not be
                cached as "not installed".
        """
        what = f"{ref.plural}.{ref.group}/{ref.version}"
        target = (
            f"{what} {name}" if name else f"{what} in {namespace or '<all namespaces>'}"
        )

        def _read() -> Sequence[Mapping[str, Any]]:
            apis = self._apis()
            timeout = self._timeout_seconds()
            with _absent_is_empty(target, apis.kube_context):
                if name is not None and namespace is not None:
                    one = apis.custom.get_namespaced_custom_object(
                        ref.group,
                        ref.version,
                        namespace,
                        ref.plural,
                        name,
                        _request_timeout=timeout,
                    )
                elif name is not None:
                    one = apis.custom.get_cluster_custom_object(
                        ref.group,
                        ref.version,
                        ref.plural,
                        name,
                        _request_timeout=timeout,
                    )
                elif namespace is not None:
                    return _items(
                        apis.sanitize(
                            apis.custom.list_namespaced_custom_object(
                                ref.group,
                                ref.version,
                                namespace,
                                ref.plural,
                                label_selector=selector or "",
                                _request_timeout=timeout,
                            )
                        )
                    )
                else:
                    return _items(
                        apis.sanitize(
                            apis.custom.list_cluster_custom_object(
                                ref.group,
                                ref.version,
                                ref.plural,
                                label_selector=selector or "",
                                _request_timeout=timeout,
                            )
                        )
                    )
                body = _as_mapping(apis.sanitize(one))
                return [body] if body else []
            return []

        return await run_in_thread(_read)

    async def crd_schema(self, ref: ResourceRef) -> Mapping[str, Any] | None:
        """Return the CRD's OpenAPI schema for *ref*'s version, or ``None``.

        ``None`` means "this coordinate is not served here" — either the CRD is
        absent, or it exists but does not serve ``ref.version``. Both are the
        answer a caller needs before naming a label key that only some controller
        builds emit: naming ``temporal.io/variant`` on a build that does not emit
        it matches nothing, which is indistinguishable from a correct negative.

        Args:
            ref: API coordinates of the kind to look up.

        Returns:
            The version's ``openAPIV3Schema``, or ``None``.

        Raises:
            ClusterReadFailedError: On anything that is not a clean 404 — the
                same narrowing as :meth:`custom_resources`, and the reason it
                exists: a 403 cached as "not installed" is a false negative that
                no later read corrects.
        """
        crd_name = f"{ref.plural}.{ref.group}"
        target = f"CRD {crd_name}"

        def _read() -> Mapping[str, Any] | None:
            apis = self._apis()
            with _absent_is_empty(target, apis.kube_context):
                crd = apis.crds.read_custom_resource_definition(
                    crd_name, _request_timeout=self._timeout_seconds()
                )
                return _versioned_schema(apis.sanitize(crd), ref.version, crd_name)
            return None

        return await run_in_thread(_read)

    # -- beyond the Protocols ----------------------------------------------

    async def container_log(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        since: timedelta | None = None,
        previous: bool = False,
        tail_lines: int | None = -1,
    ) -> str:
        """Read one container's output as text.

        Not on :class:`ClusterReader`: that Protocol's ``logs()`` is deliberately
        selector-shaped, and a per-container read exists for the one thing a
        merged stream cannot express — writing an evidence file per container,
        including the *previous* container's output after a restart, which is
        where a crash loop's actual cause is.

        Args:
            namespace: Namespace the pod is in.
            pod: Pod name.
            container: Container name within the pod.
            since: Only lines newer than this.
            previous: Read the previous terminated container instead of the
                running one.
            tail_lines: Cap on returned lines. ``-1`` (the default) means "use
                this reader's ``tail_lines``"; ``None`` means no cap. The
                sentinel exists because ``None`` is itself a meaningful value
                here, so it cannot double as "unset".

        Returns:
            The container's output. Empty when there is none — a container that
            has logged nothing, or a ``previous`` read on a container that has
            never restarted, are both clean 404s from the API server.

        Raises:
            ClusterReadFailedError: On any other failure.
        """
        target = f"logs for {namespace}/{pod}/{container}"
        cap = self._tail_lines if tail_lines == -1 else tail_lines

        def _read() -> str:
            apis = self._apis()
            kwargs: dict[str, Any] = {
                "container": container,
                "timestamps": True,
                "previous": previous,
                "_request_timeout": self._timeout_seconds(),
            }
            if cap is not None:
                kwargs["tail_lines"] = cap
            if since is not None:
                kwargs["since_seconds"] = max(1, int(since.total_seconds()))
            with _absent_is_empty(target, apis.kube_context):
                text = apis.core.read_namespaced_pod_log(pod, namespace, **kwargs)
                if isinstance(text, str):
                    return text
                logger.warning(
                    "%s came back as %s rather than text — reporting no output",
                    target,
                    type(text).__name__,
                )
                return ""
            return ""

        return await run_in_thread(_read)


# ---------------------------------------------------------------------------
# 404-is-an-answer, for the reads where that is true
# ---------------------------------------------------------------------------


@contextmanager
def _absent_is_empty(target: str, kube_context: str | None) -> Iterator[None]:
    """Swallow a clean 404 and convert every other API error to a typed leaf.

    The narrowing is the whole point, so it is one function used by all three
    reads that need it rather than three ``except`` blocks that can drift. A
    ``return`` inside the ``with`` block returns from the enclosing function; a
    404 falls out of the block instead, and the caller's line after it supplies
    the "absent" answer.

    Args:
        target: What was being read, as a noun phrase for the report.
        kube_context: Context the read went through, or ``None``.

    Yields:
        Nothing. The block's own ``return`` is the found case; the line after the
        block is the absent one.
    """
    try:
        yield
    except _api_exception() as error:
        status = _status_of(error)
        if status == 404:
            logger.debug("%s is not present on this cluster (HTTP 404)", target)
            return
        raise ClusterReadFailedError(
            message=(
                f"Could not read {target}"
                + (f" (HTTP {status})" if status is not None else "")
                + " — not treating this as absent, because a 403, an expired "
                "token or a timeout is a read that failed rather than a "
                "resource that is missing"
            ),
            target=target,
            status=status,
            kube_context=kube_context,
            cause=error,
        ) from error


# ---------------------------------------------------------------------------
# Sanitized dicts -> typed states
# ---------------------------------------------------------------------------


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    """A sanitized blob as a string-keyed mapping, or ``None`` if it is not one.

    One ``cast`` for the whole module. ``sanitize_for_serialization`` is typed as
    returning any JSON scalar or container, so every field read downstream would
    otherwise be against an unknown key type — and the point of sanitizing was to
    get *dicts with the manifest's own keys*, which this states once.
    """
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _items(listing: object) -> Sequence[Mapping[str, Any]]:
    """The ``items`` of a sanitized list response, or empty for a shapeless one."""
    body = _as_mapping(listing)
    if body is None:
        return []
    items = body.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in (_as_mapping(entry) for entry in items) if item]


def _section(body: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One top-level section of a resource body, as a mapping even when absent."""
    return _as_mapping(body.get(key)) or {}


def _int_at(section: Mapping[str, Any], key: str) -> int:
    """An integer field, with a missing or non-numeric value read as ``0``.

    ``.status.readyReplicas`` is *omitted* rather than zeroed by the API server
    while a Deployment has no ready replicas, so "absent" and "zero" genuinely
    are the same reading for these counts — unlike an unreadable cluster, which
    raises long before this.
    """
    value = section.get(key)
    return value if isinstance(value, int) else 0


def _deployment_state(body: Mapping[str, Any], namespace: str) -> DeploymentState:
    """One sanitized Deployment as a :class:`DeploymentState`."""
    metadata = _section(body, "metadata")
    spec = _section(body, "spec")
    status = _section(body, "status")
    name = metadata.get("name")
    return DeploymentState(
        name=name if isinstance(name, str) else "",
        namespace=_namespace_of(metadata, namespace),
        desired_replicas=_int_at(spec, "replicas"),
        ready_replicas=_int_at(status, "readyReplicas"),
        updated_replicas=_int_at(status, "updatedReplicas"),
    )


def _pod_state(body: Mapping[str, Any], namespace: str) -> PodState:
    """One sanitized pod as a :class:`PodState`.

    ``ready`` requires at least one container status. The ``kubectl`` version
    this replaces used a bare ``all(...)``, which is ``True`` over an empty
    sequence — so a freshly-created pod that had not reported a single container
    yet read as fully ready, which is exactly the shape a "the worker is up"
    assertion must not accept.
    """
    metadata = _section(body, "metadata")
    spec = _section(body, "spec")
    status = _section(body, "status")
    statuses = status.get("containerStatuses")
    container_statuses = [
        cs
        for cs in (
            _as_mapping(entry)
            for entry in (statuses if isinstance(statuses, list) else [])
        )
        if cs
    ]
    containers = {
        name: _int_at(cs, "restartCount")
        for cs, name in ((cs, cs.get("name")) for cs in container_statuses)
        if isinstance(name, str)
    }
    labels = _as_mapping(metadata.get("labels"))
    node = spec.get("nodeName")
    name = metadata.get("name")
    return PodState(
        name=name if isinstance(name, str) else "",
        namespace=_namespace_of(metadata, namespace),
        phase=_pod_phase(status.get("phase")),
        ready=bool(container_statuses)
        and all(cs.get("ready") is True for cs in container_statuses),
        restarts=sum(containers.values()),
        node=node if isinstance(node, str) else None,
        labels={k: str(v) for k, v in labels.items()} if labels else None,
        containers=containers or None,
    )


def _namespace_of(metadata: Mapping[str, Any], fallback: str) -> str:
    """``metadata.namespace``, falling back to the namespace that was asked for."""
    namespace = metadata.get("namespace")
    return namespace if isinstance(namespace, str) else fallback


def _pod_phase(value: Any) -> PodPhase:
    """A reported phase as a :class:`PodPhase`, unrecognised values as ``UNKNOWN``.

    A phase this SDK has never heard of is precisely what ``Unknown`` means, and
    raising on it would turn a new upstream phase name into a harness crash. A
    lookup rather than ``PodPhase(value)`` in a ``try``: an unrecognised phase is
    a classification, not a failure, so there is no exception worth a stack trace.
    """
    phase = _PHASES.get(value) if isinstance(value, str) else None
    if phase is not None:
        return phase
    logger.warning(
        "Unrecognised pod phase %r — reading it as %s", value, PodPhase.UNKNOWN
    )
    return PodPhase.UNKNOWN


def _versioned_schema(
    crd: object, version: str, crd_name: str
) -> Mapping[str, Any] | None:
    """The ``openAPIV3Schema`` a CRD serves for *version*, or ``None``.

    A CRD that exists but does not serve the asked-for version is the same answer
    as one that is absent: nothing at these coordinates. Logged at ``WARNING``
    rather than passed over silently, because it is the more surprising of the
    two — the kind is installed, so a caller reasonably expected a schema.
    """
    body = _as_mapping(crd)
    if body is None:
        return None
    versions = _section(body, "spec").get("versions")
    for raw in versions if isinstance(versions, list) else []:
        entry = _as_mapping(raw)
        if entry is None or entry.get("name") != version:
            continue
        return _as_mapping(_section(entry, "schema").get("openAPIV3Schema"))
    logger.warning(
        "%s is installed but serves no version %r — reporting no schema",
        crd_name,
        version,
    )
    return None


def _log_lines(pod: str, container: str, text: str) -> Iterator[LogLine]:
    """Split a ``timestamps=True`` log read into :class:`LogLine` values."""
    for raw in text.splitlines():
        if not raw:
            continue
        timestamp, message = _split_timestamp(raw)
        yield LogLine(
            pod=pod, container=container, message=message, timestamp=timestamp
        )


def _sortable(lines: Iterator[LogLine]) -> list[tuple[tuple[datetime, int], LogLine]]:
    """Key one container's lines so a k-way merge can order them across pods.

    Two properties the key has to hold, and neither is free:

    *An untimestamped line stays attached to the line it continues.* A stack
    trace arrives as one timestamped line followed by frames with no prefix of
    their own; sorting those to the front (or dropping them to the end) would
    scatter a traceback across every other pod's output. So a line with no
    timestamp inherits the last one seen **in its own stream**, which keeps the
    frames adjacent to their header wherever that header lands.

    *Each stream is sorted by its own key.* ``heapq.merge`` merges sorted inputs
    only. The inherited timestamp is non-decreasing and the position breaks ties
    within a stream, so it is — and the position also makes the order total, so
    two pods logging in the same millisecond merge deterministically instead of
    by whichever read finished first.

    Leading lines with no timestamp at all inherit ``datetime.min``: they were
    logged before anything this read can date, and sorting them first is the only
    honest answer.
    """
    keyed: list[tuple[tuple[datetime, int], LogLine]] = []
    carried = datetime.min.replace(tzinfo=UTC)
    for position, line in enumerate(lines):
        if line.timestamp is not None:
            carried = line.timestamp
        keyed.append(((carried, position), line))
    return keyed


def _split_timestamp(line: str) -> tuple[datetime | None, str]:
    """Peel the RFC3339 prefix ``timestamps=True`` adds, if it is there.

    Nanosecond precision is truncated to microseconds: the API server emits nine
    fractional digits and :meth:`datetime.fromisoformat` accepts at most six. A
    line whose prefix does not parse keeps its full text — losing the message to
    a formatting surprise is worse than losing the timestamp.
    """
    head, _, rest = line.partition(" ")
    if "T" not in head:
        return None, line
    normalised = head.removesuffix("Z")
    date_part, dot, fraction = normalised.partition(".")
    if dot:
        normalised = f"{date_part}.{fraction[:6]}"
    try:
        return datetime.fromisoformat(f"{normalised}+00:00"), rest
    except ValueError:
        # conformance: ignore[E007] an unparsable prefix is a classification — "this line carries no timestamp" — not a swallowed failure; a container that logs without timestamps would turn a per-line log into a flood
        return None, line


def _json_or_none(response: httpx.Response) -> Any | None:
    """A response's decoded JSON, or ``None`` when it has none.

    An error page is the most useful thing to report when a call fails, and it is
    rarely JSON — so a decode failure leaves ``body`` empty and lets
    :attr:`HttpResponse.text` carry it, instead of losing the response.
    """
    try:
        return response.json()
    except ValueError:
        # ``httpx`` decodes with the standard library, whose ``JSONDecodeError``
        # is a ``ValueError`` — narrow enough to be exact, wide enough to cover
        # an empty body and a malformed one alike.
        logger.debug(
            "Response body is not JSON — reporting it as text only", exc_info=True
        )
        return None
