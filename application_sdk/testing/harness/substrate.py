"""Where the harness is running from — the fourth point of variance, named.

FND-224 catalogued three things the two source design documents disagreed about
and therefore could not be assumed shared: tenant wiring, app wiring, and how a
run starts work. Building the fixtures found a fourth that **neither document
names**: the execution substrate.

The connector harness drives a docker-compose worker on ``localhost:8000``,
gated on ``/server/health``, on a per-leg task queue. There is no Kubernetes API
in that picture at all — the CI runner has no route into the tenant vcluster,
which is also why the AE submit is the only tenant-facing probe of the installed
app pod. The runtime scenario suite wants the opposite: an in-cluster or
kubeconfig-driven reader against a dev tenant, where cluster reads are the
primary evidence.

So substrate is a *declared* input, not a detected one. Detection is the tempting
alternative and it is wrong in a specific way: "is there a kubeconfig?" is almost
always yes on a developer's machine, and answering it by reaching for whichever
cluster ``kubectl`` last pointed at is how a local test run reads a production
tenant. A suite says which substrate it is on, and a substrate that cannot
support a read refuses it by name.

Kept out of :mod:`application_sdk.testing.harness.fixtures` so a composer that
does not use pytest — the runtime suite drives the harness from sync scenario
code through :func:`~application_sdk.testing.harness.bridge.run_sync` — can
select a reader without importing a test framework.
"""

from __future__ import annotations

from enum import StrEnum

from application_sdk.testing.harness._errors import (
    HarnessNotBuiltError,
    SubstrateHasNoClusterError,
)
from application_sdk.testing.harness.cluster import ClusterReader, KubernetesReader

__all__ = ["Substrate", "cluster_reader_for"]


class Substrate(StrEnum):
    """Where the code driving the harness is running, relative to the app.

    Three values because the credential source differs three ways, which is the
    only thing the harness has to know: a local process has none, an
    out-of-cluster driver reads a kubeconfig, and an in-cluster driver reads its
    own ServiceAccount.
    """

    #: A worker reachable over ``localhost`` — the connector CI substrate. There
    #: is no cluster to read, and saying so is the point: see
    #: :class:`~application_sdk.testing.harness._errors.SubstrateHasNoClusterError`.
    LOCAL = "local"
    #: An out-of-cluster driver, credentials from the ambient kubeconfig. What
    #: the runtime scenario suite runs on today, over a VPN and a vcluster
    #: tunnel.
    KUBECONFIG = "kubeconfig"
    #: A driver running inside the cluster it reads, credentials from its own
    #: ServiceAccount. FND-248; the reader is the same class, with a different
    #: API-bundle factory.
    IN_CLUSTER = "in_cluster"


def cluster_reader_for(
    substrate: Substrate, *, kube_context: str | None = None
) -> ClusterReader:
    """Build the cluster reader *substrate* implies.

    Args:
        substrate: The declared substrate.
        kube_context: Kubeconfig context for
            :attr:`Substrate.KUBECONFIG`, or ``None`` for the kubeconfig's
            current one. Ignored by the other substrates.

    Returns:
        A reader. Nothing is connected here — :class:`KubernetesReader` builds
        its API bundle per thread on first read, so an unusable kubeconfig
        surfaces at the read rather than at construction.

    Raises:
        SubstrateHasNoClusterError: On :attr:`Substrate.LOCAL`. A composer that
            reached here on the local substrate wants either a different
            substrate or no cluster read; both are better than a reader that
            fails on first use, and far better than one that silently reads
            whichever cluster the ambient kubeconfig names.
        HarnessNotBuiltError: On :attr:`Substrate.IN_CLUSTER`, until FND-248
            lands the in-cluster API-bundle factory.
    """
    if substrate is Substrate.KUBECONFIG:
        return KubernetesReader(kube_context=kube_context)
    if substrate is Substrate.IN_CLUSTER:
        raise HarnessNotBuiltError(
            message=(
                "the in-cluster credential factory is not implemented yet — run "
                "with Substrate.KUBECONFIG, or supply your own ClusterReader"
            ),
            operation="cluster_reader_for",
            reason="FND-248 lands the in-cluster API-bundle factory",
            issue="FND-248",
            component="harness_substrate",
        )
    raise SubstrateHasNoClusterError(
        message=(
            f"substrate {substrate} has no Kubernetes API to read: the connector "
            "harness drives a worker on localhost and the CI runner has no route "
            "into a cluster. Declare Substrate.KUBECONFIG (or IN_CLUSTER) if this "
            "suite really does read a cluster, or drop the cluster read."
        ),
        substrate=str(substrate),
    )
