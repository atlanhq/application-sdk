"""Typed error leaves for the cluster readers.

Private module: the leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness.cluster`. Mirrors
:mod:`application_sdk.testing.harness._errors`.

Three leaves, one per thing that can go wrong *before* a verdict exists:

* the typed client is not installed (:class:`KubernetesExtraMissingError`),
* there is no usable kubeconfig to build it from
  (:class:`KubeconfigUnavailableError`),
* the API server answered, but not with an answer
  (:class:`ClusterReadFailedError`).

None of them is a verdict about the thing under test, which is why all three are
raised rather than folded into a return value. A bounded wait turns them into
:class:`~application_sdk.testing.harness.outcome.Indeterminate` through its
transient classifier; a one-shot read lets them propagate. The one thing none of
them may become is an empty result — that is the C4 fail-open shape this child
deletes, and the reason ``get_pods``' ``return []`` on a ``kubectl`` failure is
gone rather than ported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError, PreconditionError

__all__ = [
    "ClusterReadFailedError",
    "KubeconfigUnavailableError",
    "KubernetesExtraMissingError",
]


@dataclass(kw_only=True)
class KubernetesExtraMissingError(PreconditionError):
    """The typed Kubernetes client is not installed in this environment.

    ``PRECONDITION`` rather than ``DEPENDENCY_UNAVAILABLE``: nothing is down and
    no retry helps — an install has to happen first, which is the category's own
    litmus test. Raised at construction rather than at import so importing
    :mod:`application_sdk.testing.harness.cluster` stays free for a connector
    that never touches a cluster.

    Attributes:
        extra: The extra to install, named in the message as well as carried as
            a field so a report can group on it.
    """

    code: ClassVar[str] = "PRECONDITION_KUBERNETES_EXTRA_MISSING"
    component: str | None = "harness_cluster"
    extra: str | None = "harness"


@dataclass(kw_only=True)
class KubeconfigUnavailableError(PreconditionError):
    """No usable kubeconfig, or the named context is not in it.

    Distinct from :class:`ClusterReadFailedError` because the fix is different
    and local: point ``KUBECONFIG`` at the right file, or log the vcluster in.
    Nothing was read, and nothing about the cluster is known — including whether
    it is reachable.

    Attributes:
        kube_context: The context that was asked for, or ``None`` for whichever
            one the kubeconfig marks current.
    """

    code: ClassVar[str] = "PRECONDITION_KUBECONFIG_UNAVAILABLE"
    component: str | None = "harness_cluster"
    kube_context: str | None = None


@dataclass(kw_only=True)
class ClusterReadFailedError(DependencyUnavailableError):
    """A cluster read reached the API server and did not come back with data.

    ``DEPENDENCY_UNAVAILABLE`` for the same reason
    :class:`~application_sdk.testing.harness._errors.WaitIndeterminateError` is:
    an expired token, a 403 from a narrowed role or a dropped tunnel is neither a
    pass nor a regression in the thing under test, and this is the category whose
    definition is "the same call would work once the dependency recovers".

    A ``404`` is deliberately **not** one of these on a custom-resource read: an
    absent CRD is a real, readable answer ("not installed here"). It *is* one of
    these on a built-in read, because a missing namespace is a setup fault, not
    an empty match — and returning an empty list for it is exactly how an Atlas
    outage came to be reported as a missing asset.

    Attributes:
        target: What was being read, as a noun phrase ("pods in ns/foo") — goes
            straight into the report.
        status: HTTP status the API server returned, when there was one. ``None``
            for a transport failure that never got a response.
        kube_context: Kubeconfig context the read went through, so a run against
            the wrong cluster is visible in the failure rather than inferred.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_CLUSTER_READ_FAILED"
    component: str | None = "harness_cluster"
    status: int | None = None
    kube_context: str | None = None
