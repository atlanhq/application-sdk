"""Read-only Protocols over a Kubernetes cluster, plus the states they return.

**Read-only by decision, not by omission.** The runtime scenario suite mutates —
patching a TWD to compress timings, suspending and resuming a Flux HelmRelease
so the patch survives, rendering and server-side-applying charts, installing a
fixture app. All of that stays on the suite side (agreed on FND-224, 2026-08-17):
the SDK has no app-functional need for it, so abstracting it now would be
inventing a shared surface for one consumer. Every Protocol here is a reader, and
:class:`KubernetesReader` reaches for ``list``/``read``/``get`` verbs only, so
neither can be quietly turned into a writer.

**Two Protocols, not one union.** :class:`ClusterReader` covers the built-in
kinds the harness itself needs. Custom resources go on
:class:`CustomResourceReader`, parameterised by :class:`ResourceRef` rather than
by an enum of kinds plus a plurals map — an enum would mean every new consumer's
kind becomes a change in *this* repo, which is precisely the widening FND-224
exists to remove.

**Why a Protocol from day one.** ``kubectl`` is explicitly not the end state
(HOR-818 for Horizon, plus the agent-infrastructure gateway). Each of those
arrives as a backend rather than as a rewrite of every caller. FND-248's
in-cluster backend is the nearest one, and it is smaller than a backend: the only
thing it changes is where the credentials come from, which is a factory passed to
:class:`KubernetesReader` rather than a second reader.

**One backend, and it is not ``kubectl``.** :class:`KubernetesReader` is the
in-process typed client (FND-241, behind the optional ``harness`` extra). The
``kubectl``-shelling reads it replaces — ``testing/e2e/pods.py``'s ``get_pods``,
``get_pod_logs`` and ``wait_for_pods_ready`` — are **deleted** rather than kept
as a second backend: they had no caller outside this repo, and carrying two
implementations would have meant maintaining their fail-open ``return []`` too.

``kubectl port-forward`` survives as *transport* only: it is what reaches an app
handler Service from outside the cluster, ``kubernetes.stream``'s port-forward is
a socket-level API rather than a drop-in, and
:meth:`ClusterReader.http` sits on it. It moved here with the backend (it is
re-exported from ``testing/e2e/portforward`` unchanged) so a harness module never
has to import from the package child H re-expresses over it.

Module map:

``_states``
    The typed states and requests — :class:`PodState`, :class:`DeploymentState`,
    :class:`LogLine`, :class:`ServiceTarget`, :class:`HttpRequest`,
    :class:`HttpResponse`, :class:`ResourceRef`.
``_protocols``
    :class:`ClusterReader` and :class:`CustomResourceReader`.
``kube``
    :class:`KubernetesReader` and the :func:`kubeconfig_apis` factory.
``_portforward``
    The one surviving ``kubectl`` call, as transport.
``_errors``
    The three leaves a read can raise before there is a verdict.
"""

from application_sdk.testing.harness.cluster._errors import (
    ClusterReadFailedError,
    KubeconfigUnavailableError,
    KubernetesExtraMissingError,
)
from application_sdk.testing.harness.cluster._portforward import (
    PortForward,
    kube_http_call,
    port_forward,
)
from application_sdk.testing.harness.cluster._protocols import (
    ClusterReader,
    CustomResourceReader,
)
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
from application_sdk.testing.harness.cluster.kube import (
    KubernetesApis,
    KubernetesReader,
    kubeconfig_apis,
)

__all__ = [
    # Protocols
    "ClusterReader",
    "CustomResourceReader",
    # States and requests
    "DeploymentState",
    "HttpRequest",
    "HttpResponse",
    "LogLine",
    "PodPhase",
    "PodState",
    "ResourceRef",
    "ServiceTarget",
    # The typed backend
    "KubernetesApis",
    "KubernetesReader",
    "kubeconfig_apis",
    # Transport — the one surviving kubectl call
    "PortForward",
    "kube_http_call",
    "port_forward",
    # Leaves
    "ClusterReadFailedError",
    "KubeconfigUnavailableError",
    "KubernetesExtraMissingError",
]
