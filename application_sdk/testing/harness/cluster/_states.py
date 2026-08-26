"""The typed states the cluster readers return, and the requests they take.

Split out of the package ``__init__`` when the typed backend landed (FND-241):
the backend module has to import these, and a package whose ``__init__`` both
defines them *and* imports the backend is a cycle. Every name here is re-exported
from :mod:`application_sdk.testing.harness.cluster`, which stays the import path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

__all__ = [
    "DeploymentState",
    "HttpRequest",
    "HttpResponse",
    "LogLine",
    "PodPhase",
    "PodState",
    "ResourceRef",
    "ServiceTarget",
]


class PodPhase(str, Enum):
    """Kubernetes pod phase, as the API server reports it."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class PodState:
    """One pod, reduced to what a harness assertion reads.

    Attributes:
        name: Pod name.
        namespace: Namespace the pod is in.
        phase: Reported phase.
        ready: Whether every container passes its readiness probe. Not derivable
            from :attr:`phase` — a ``Running`` pod with a failing readiness probe
            is the exact shape a "worker is up" assertion must not accept.
        restarts: Total container restarts. A worker that is up but restarting is
            a different failure from a worker that never came up.
        node: Node the pod is scheduled on, or ``None`` while unscheduled.
        labels: Pod labels, for a caller narrowing further than its selector did.
        containers: Container name -> that container's restart count, or ``None``
            when the pod has reported no container statuses yet. Carried
            alongside the :attr:`restarts` total because the total says a pod is
            restarting and this says *which container* is — and because reading
            one container's output at all needs its name.
    """

    name: str
    namespace: str
    phase: PodPhase
    ready: bool
    restarts: int
    node: str | None = None
    labels: Mapping[str, str] | None = None
    containers: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentState:
    """One Deployment's replica counts.

    Attributes:
        name: Deployment name.
        namespace: Namespace the Deployment is in.
        desired_replicas: ``.spec.replicas`` — the *intent*. This is the scaling
            metric, deliberately: ``.status.readyReplicas`` lags, so asserting on
            it turns a scaling assertion into a race against pod startup.
        ready_replicas: ``.status.readyReplicas`` — still exposed, because "did
            the scale-up actually land" is a real and different question.
        updated_replicas: ``.status.updatedReplicas``, for telling a rollout in
            progress from a settled one.
    """

    name: str
    namespace: str
    desired_replicas: int
    ready_replicas: int
    updated_replicas: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LogLine:
    """One line of container output.

    Attributes:
        pod: Pod the line came from. A selector matches many pods, and which one
            logged a line is usually the point.
        container: Container within that pod.
        message: The line itself, newline stripped.
        timestamp: Server-side timestamp, when the backend supplies one.
    """

    pod: str
    container: str
    message: str
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceTarget:
    """A Service and port to reach, however the backend chooses to reach it.

    Attributes:
        namespace: Namespace the Service is in.
        service: Service name.
        port: Service port.
    """

    namespace: str
    service: str
    port: int


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpRequest:
    """An HTTP call to make against a :class:`ServiceTarget`.

    Attributes:
        method: HTTP method.
        path: Path including any query string, leading slash included.
        body: JSON body to send, or ``None`` for no body.
        headers: Extra headers.
        timeout: Per-request bound. Distinct from the enclosing wait's budget:
            one hung request must not consume the whole budget silently.
    """

    method: str
    path: str
    body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None
    timeout: timedelta = timedelta(seconds=30)


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpResponse:
    """What came back.

    Attributes:
        status: HTTP status code.
        body: Decoded JSON body, or ``None`` when the body was empty or not JSON.
        text: Raw body text. Retained alongside :attr:`body` because a non-JSON
            error page is the most useful thing to report when a call fails.
    """

    status: int
    body: Any | None = None
    text: str = ""


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Identifies a custom-resource kind by its API coordinates.

    Parameterised rather than enumerated on purpose: a ``Kind`` enum plus a
    ``_PLURALS`` map means every consumer's new kind is a change in this repo,
    for a need only that consumer has. With a ref, the calling suite keeps its
    own kind vocabulary and resolves it to these three strings at the call.

    Attributes:
        group: API group, e.g. ``"helm.toolkit.fluxcd.io"``.
        version: API version, e.g. ``"v2"``.
        plural: Plural resource name as the API server routes it, e.g.
            ``"helmreleases"``. The plural, not the kind: it is what the URL path
            needs, and deriving it from the kind is the map this avoids.
    """

    group: str
    version: str
    plural: str
