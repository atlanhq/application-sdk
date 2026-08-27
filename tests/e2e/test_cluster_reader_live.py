"""Live-cluster smoke test for the typed cluster reader.

FND-241's acceptance criterion is "the typed reader works", and unit tests with a
scripted client cannot establish that: what they cannot check is that
``load_kube_config`` resolves an ``exec`` credential plugin (the vcluster case),
that the field paths this backend reads are the ones a real API server emits, and
that a read against a namespace nobody has *raises* rather than answering empty.

So it lives here as an explicit, opt-in check rather than as prose in a PR
description. Requires VPN plus a logged-in ``kubectl`` context, which is why it is
``e2e``-marked and skipped without a namespace to point at::

    E2E_CLUSTER_NAMESPACE=app-hello-world \\
        uv run pytest tests/e2e/test_cluster_reader_live.py -m e2e -v

Set ``E2E_KUBE_CONTEXT`` to pin a context; without it the kubeconfig's current
one is used.
"""

from __future__ import annotations

import os

import pytest

from application_sdk.testing.harness.cluster import (
    ClusterReadFailedError,
    DeploymentState,
    PodState,
    ResourceRef,
)

pytest.importorskip("kubernetes", reason="requires the `harness` extra")

from application_sdk.testing.harness.cluster import KubernetesReader  # noqa: E402

pytestmark = pytest.mark.e2e

_NAMESPACE_ENV = "E2E_CLUSTER_NAMESPACE"


@pytest.fixture(scope="module")
def namespace() -> str:
    value = os.environ.get(_NAMESPACE_ENV)
    if not value:
        pytest.skip(f"set {_NAMESPACE_ENV} to a namespace to read")
    return value


@pytest.fixture(scope="module")
def reader() -> KubernetesReader:
    return KubernetesReader(kube_context=os.environ.get("E2E_KUBE_CONTEXT"))


async def test_pods_read_from_a_real_api_server(
    reader: KubernetesReader, namespace: str
) -> None:
    """Every field the backend claims to read is present on a real pod."""
    pods = await reader.pods(namespace)

    assert pods, f"no pods in {namespace} — point at a namespace that has some"
    for pod in pods:
        assert isinstance(pod, PodState)
        assert pod.name
        assert pod.namespace == namespace
        assert pod.restarts >= 0
        # A pod reporting container statuses must report them by name: that is
        # what a per-container log read needs, and the mapping is derived rather
        # than passed through.
        if pod.containers is not None:
            assert all(name for name in pod.containers)
            assert pod.restarts == sum(pod.containers.values())


async def test_deployments_expose_spec_replicas(
    reader: KubernetesReader, namespace: str
) -> None:
    deployments = await reader.deployments(namespace)

    assert deployments, f"no Deployments in {namespace}"
    for deployment in deployments:
        assert isinstance(deployment, DeploymentState)
        assert deployment.desired_replicas >= 0
        # readyReplicas lags spec.replicas, never leads it
        assert deployment.ready_replicas <= deployment.desired_replicas


async def test_logs_stream_from_a_real_container(
    reader: KubernetesReader, namespace: str
) -> None:
    """A read that returns no lines is fine; one that crashes on real output is not."""
    lines = []
    async for line in reader.logs(namespace):
        lines.append(line)
        if len(lines) >= 5:
            break

    for line in lines:
        assert line.pod
        assert line.container
        # The RFC3339 prefix is peeled, so the message never starts with it
        assert not line.message.startswith("20")


async def test_a_namespace_nobody_has_raises_rather_than_reading_empty(
    reader: KubernetesReader,
) -> None:
    """The C4 fix, against a real API server: 404 is not an empty match."""
    with pytest.raises(ClusterReadFailedError) as raised:
        await reader.pods("fnd241-no-such-namespace")

    assert raised.value.status in (403, 404)


async def test_an_uninstalled_crd_is_absent_rather_than_an_error(
    reader: KubernetesReader,
) -> None:
    """The other half of the narrowing: a clean 404 *is* a readable answer."""
    absent = ResourceRef("fnd241.example.com", "v1", "notathings")

    assert await reader.crd_schema(absent) is None
    assert await reader.custom_resources(absent, namespace="default") == []
