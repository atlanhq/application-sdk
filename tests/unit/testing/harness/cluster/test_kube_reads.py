"""Unit tests for the typed Kubernetes reader.

These are the tests ``testing/e2e/pods.py`` never had. Two of them pin bugs the
``kubectl`` version shipped with: an empty ``all(...)`` reading a pod with no
container statuses as fully ready, and a failed read returning an empty list.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.testing.harness.cluster import (
    ClusterReader,
    ClusterReadFailedError,
    CustomResourceReader,
    HttpRequest,
    HttpResponse,
    KubeconfigUnavailableError,
    KubernetesExtraMissingError,
    PodPhase,
    ResourceRef,
    ServiceTarget,
)
from application_sdk.testing.harness.cluster.kube import (
    KubernetesReader,
    kubeconfig_apis,
)
from tests.unit.testing._cluster_fakes import (
    ApiException,
    FakeApis,
    deployment_body,
    pod_body,
    reader_over,
)

_TWD = ResourceRef("temporal.io", "v1alpha1", "temporalworkerdeployments")


def _listing(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items)}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_the_reader_satisfies_both_protocols():
    """One backend, both Protocols — a consumer needing one asks for one."""
    reader = reader_over(FakeApis())
    assert isinstance(reader, ClusterReader)
    assert isinstance(reader, CustomResourceReader)


# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------


async def test_pods_carry_what_a_readiness_assertion_reads():
    apis = FakeApis(
        pods=_listing(
            pod_body(
                "worker-0",
                containers=[
                    {"name": "worker", "ready": True, "restartCount": 2},
                    {"name": "sidecar", "ready": True, "restartCount": 1},
                ],
                labels={"app": "worker"},
            )
        )
    )
    pods = await reader_over(apis).pods("connectors", "app=worker")

    assert len(pods) == 1
    pod = pods[0]
    assert pod.name == "worker-0"
    # metadata.namespace is absent from the body: the namespace asked for stands in
    assert pod.namespace == "connectors"
    assert pod.phase is PodPhase.RUNNING
    assert pod.ready is True
    assert pod.restarts == 3
    assert pod.node == "node-a"
    assert pod.labels == {"app": "worker"}
    assert pod.containers == {"worker": 2, "sidecar": 1}
    assert apis.kwargs_for("list_namespaced_pod")["label_selector"] == "app=worker"


async def test_a_pod_with_no_container_statuses_is_not_ready():
    """The bug the ``kubectl`` reader shipped: ``all([])`` is ``True``.

    A just-created pod that has reported no container statuses read as fully
    ready, which is exactly what a "the worker is up" assertion must not accept.
    """
    apis = FakeApis(pods=_listing(pod_body("pending-0", phase="Pending")))
    pod = (await reader_over(apis).pods("connectors"))[0]

    assert pod.ready is False
    assert pod.restarts == 0
    assert pod.containers is None


async def test_a_running_pod_with_a_failing_probe_is_not_ready():
    apis = FakeApis(
        pods=_listing(
            pod_body(
                "worker-0",
                containers=[
                    {"name": "worker", "ready": True, "restartCount": 0},
                    {"name": "sidecar", "ready": False, "restartCount": 0},
                ],
            )
        )
    )
    pod = (await reader_over(apis).pods("connectors"))[0]

    assert pod.phase is PodPhase.RUNNING
    assert pod.ready is False


async def test_an_unrecognised_phase_reads_as_unknown():
    """A phase this SDK has not heard of is what ``Unknown`` means."""
    apis = FakeApis(pods=_listing(pod_body("odd-0", phase="Rebooting")))
    pod = (await reader_over(apis).pods("connectors"))[0]

    assert pod.phase is PodPhase.UNKNOWN


async def test_an_unscheduled_pod_has_no_node():
    apis = FakeApis(pods=_listing(pod_body("waiting-0", phase="Pending", node=None)))
    pod = (await reader_over(apis).pods("connectors"))[0]

    assert pod.node is None
    assert pod.labels is None


async def test_a_shapeless_listing_is_empty_rather_than_a_crash():
    """A response that is not a list object yields no pods, not an exception."""
    for answer in ("not a listing", {"items": "not a list"}, {}):
        apis = FakeApis(pods=answer)
        assert await reader_over(apis).pods("connectors") == []


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


async def test_deployments_expose_spec_replicas_as_the_scaling_metric():
    """``.spec.replicas`` is the intent; ``readyReplicas`` lags behind it."""
    apis = FakeApis(
        deployments=_listing(
            deployment_body("worker", desired=5, ready=2, updated=3, namespace="conn")
        )
    )
    deployments = await reader_over(apis).deployments("conn", "app=worker")

    assert len(deployments) == 1
    assert deployments[0].name == "worker"
    assert deployments[0].namespace == "conn"
    assert deployments[0].desired_replicas == 5
    assert deployments[0].ready_replicas == 2
    assert deployments[0].updated_replicas == 3


async def test_omitted_replica_counts_read_as_zero():
    """The API server omits ``readyReplicas`` rather than zeroing it."""
    apis = FakeApis(
        deployments=_listing(
            deployment_body("worker", desired=None, ready=None, updated=None)
        )
    )
    deployment = (await reader_over(apis).deployments("conn"))[0]

    assert deployment.desired_replicas == 0
    assert deployment.ready_replicas == 0
    assert deployment.updated_replicas == 0


# ---------------------------------------------------------------------------
# It cannot fail open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_an_unreadable_listing_raises_rather_than_returning_empty(status: int):
    """Every failure, 404 included: a built-in read has no "absent" answer.

    ``get_pods`` returned ``[]`` here, which is how an unreadable cluster came to
    be graded as an empty one. A missing namespace is a setup fault, not an empty
    match.
    """
    apis = FakeApis(pods=ApiException(status=status, reason="nope"))

    with pytest.raises(ClusterReadFailedError) as raised:
        await reader_over(apis).pods("connectors", "app=worker")

    assert raised.value.status == status
    assert raised.value.kube_context == "fake-context"
    assert "pods in connectors matching app=worker" in raised.value.message
    assert isinstance(raised.value.__cause__, ApiException)


async def test_an_unreadable_deployment_listing_raises_too():
    apis = FakeApis(deployments=ApiException(status=503, reason="down"))

    with pytest.raises(ClusterReadFailedError):
        await reader_over(apis).deployments("conn")


async def test_a_wiring_bug_is_not_dressed_up_as_a_dependency_failure():
    """Only the client's own error is converted.

    A ``TypeError`` reported as ``DEPENDENCY_UNAVAILABLE`` would be absorbed by a
    bounded wait's transient budget, so a bug would burn a whole 25-minute
    window instead of failing at once.
    """
    apis = FakeApis(pods=TypeError("wrong argument"))

    with pytest.raises(TypeError):
        await reader_over(apis).pods("connectors")


async def test_a_transport_failure_carries_no_status():
    apis = FakeApis(pods=ApiException(reason="connection reset"))

    with pytest.raises(ClusterReadFailedError) as raised:
        await reader_over(apis).pods("connectors")

    assert raised.value.status is None
    assert "HTTP" not in raised.value.message


# ---------------------------------------------------------------------------
# Custom resources — and the 404-only narrowing
# ---------------------------------------------------------------------------


async def test_a_namespaced_listing_passes_the_ref_through_verbatim():
    apis = FakeApis(resources=_listing({"metadata": {"name": "twd-a"}}))
    found = await reader_over(apis).custom_resources(
        _TWD, namespace="conn", selector="app=worker"
    )

    assert [r["metadata"]["name"] for r in found] == ["twd-a"]
    assert apis.args_for("list_namespaced_custom_object") == (
        "temporal.io",
        "v1alpha1",
        "conn",
        "temporalworkerdeployments",
    )
    assert apis.kwargs_for("list_namespaced_custom_object")["label_selector"] == (
        "app=worker"
    )


async def test_no_namespace_reads_cluster_wide():
    apis = FakeApis(resources=_listing({"metadata": {"name": "twd-a"}}))
    await reader_over(apis).custom_resources(_TWD)

    assert apis.methods() == ["list_cluster_custom_object"]
    assert apis.kwargs_for("list_cluster_custom_object")["label_selector"] == ""


async def test_a_named_read_fetches_one_object_rather_than_filtering():
    """``Scaling.from_twd()`` reads one TWD by name; no label distinguishes it."""
    apis = FakeApis(resources={"metadata": {"name": "twd-a"}, "spec": {"replicas": 4}})
    found = await reader_over(apis).custom_resources(
        _TWD, namespace="conn", name="twd-a"
    )

    assert [r["spec"]["replicas"] for r in found] == [4]
    assert apis.args_for("get_namespaced_custom_object")[-1] == "twd-a"


async def test_a_named_cluster_scoped_read():
    apis = FakeApis(resources={"metadata": {"name": "cluster-thing"}})
    found = await reader_over(apis).custom_resources(_TWD, name="cluster-thing")

    assert len(found) == 1
    assert apis.methods() == ["get_cluster_custom_object"]


async def test_a_named_read_of_something_shapeless_is_empty():
    apis = FakeApis(resources="not an object")
    assert await reader_over(apis).custom_resources(_TWD, name="twd-a") == []


async def test_an_absent_kind_is_an_empty_result_not_a_failure():
    """A clean 404 is the answer "this CRD is not installed here"."""
    apis = FakeApis(resources=ApiException(status=404, reason="Not Found"))

    assert await reader_over(apis).custom_resources(_TWD, namespace="conn") == []


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_a_read_that_failed_is_never_cached_as_absent(status: int):
    """403 from a narrowed role, or an expired token, must not read as "absent".

    That false negative is the one no later read corrects — the reason the
    narrowing is 404-only.
    """
    apis = FakeApis(resources=ApiException(status=status, reason="nope"))

    with pytest.raises(ClusterReadFailedError) as raised:
        await reader_over(apis).custom_resources(_TWD, namespace="conn")

    assert raised.value.status == status
    assert "not treating this as absent" in raised.value.message


async def test_a_non_api_error_on_a_custom_read_propagates_unwrapped():
    apis = FakeApis(resources=ValueError("bad ref"))

    with pytest.raises(ValueError):
        await reader_over(apis).custom_resources(_TWD, namespace="conn")


# ---------------------------------------------------------------------------
# CRD schema
# ---------------------------------------------------------------------------


def _crd(*versions: dict[str, Any]) -> dict[str, Any]:
    return {"spec": {"versions": list(versions)}}


async def test_crd_schema_returns_the_asked_for_version():
    schema = {"properties": {"spec": {"type": "object"}}}
    apis = FakeApis(
        crd=_crd(
            {"name": "v1alpha1", "schema": {"openAPIV3Schema": schema}},
            {"name": "v1beta1", "schema": {"openAPIV3Schema": {"properties": {}}}},
        )
    )
    assert await reader_over(apis).crd_schema(_TWD) == schema
    assert apis.args_for("read_custom_resource_definition") == (
        "temporalworkerdeployments.temporal.io",
    )


async def test_crd_schema_is_none_when_the_crd_serves_another_version():
    """Installed, but not at these coordinates — the same answer as absent."""
    apis = FakeApis(crd=_crd({"name": "v2", "schema": {"openAPIV3Schema": {}}}))
    assert await reader_over(apis).crd_schema(_TWD) is None


async def test_crd_schema_is_none_when_the_version_carries_no_schema():
    apis = FakeApis(crd=_crd({"name": "v1alpha1", "schema": {}}))
    assert await reader_over(apis).crd_schema(_TWD) is None


async def test_crd_schema_is_none_for_a_shapeless_crd():
    apis = FakeApis(crd="not a crd")
    assert await reader_over(apis).crd_schema(_TWD) is None


async def test_crd_schema_is_none_when_the_crd_is_absent():
    apis = FakeApis(crd=ApiException(status=404, reason="Not Found"))
    assert await reader_over(apis).crd_schema(_TWD) is None


async def test_crd_schema_raises_on_a_forbidden_read():
    apis = FakeApis(crd=ApiException(status=403, reason="Forbidden"))

    with pytest.raises(ClusterReadFailedError):
        await reader_over(apis).crd_schema(_TWD)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

_LOG_TEXT = (
    "2026-08-26T10:00:00.123456789Z first line\n"
    "2026-08-26T10:00:01.000000000Z second line\n"
    "\n"
    "no-timestamp-here\n"
)


class _PerContainerLogs:
    """A ``read_namespaced_pod_log`` that answers differently per container."""

    def __init__(self, by_container: dict[str, str]) -> None:
        self._by_container = by_container

    def __call__(self, container: str) -> str:
        return self._by_container[container]


async def test_logs_from_two_pods_merge_on_the_server_timestamp():
    """The claim both docstrings make, pinned: merged, not pod-then-container.

    A selector-wide read is only worth more than a per-pod one if a request that
    one pod handled and another logged about comes back in the order it happened.
    Concatenating by pod would put every `web-0` line before every `web-1` line
    and lose exactly that.
    """
    apis = FakeApis(
        pods=_listing(
            pod_body("web-0", containers=[{"name": "app", "restartCount": 0}]),
            pod_body("web-1", containers=[{"name": "app", "restartCount": 0}]),
        )
    )
    per_pod = {
        "web-0": (
            "2026-08-26T10:00:00.000000000Z accepted\n"
            "2026-08-26T10:00:02.000000000Z replied\n"
        ),
        "web-1": (
            "2026-08-26T10:00:01.000000000Z forwarded\n"
            "2026-08-26T10:00:03.000000000Z done\n"
        ),
    }
    reader = reader_over(apis)
    original = reader.container_log

    async def _per_pod(namespace, pod, container, **kwargs):
        _ = await original(namespace, pod, container, **kwargs)
        return per_pod[pod]

    reader.container_log = _per_pod  # type: ignore[method-assign]

    lines = [line async for line in reader.logs("conn", "app=web")]

    assert [(line.pod, line.message) for line in lines] == [
        ("web-0", "accepted"),
        ("web-1", "forwarded"),
        ("web-0", "replied"),
        ("web-1", "done"),
    ]


async def test_an_untimestamped_line_stays_with_the_line_it_continues():
    """A stack trace must not scatter across the other pods' output.

    Frames arrive with no prefix of their own, so they inherit the last timestamp
    seen *in their own stream* — which keeps them adjacent to their header
    wherever the merge places it.
    """
    apis = FakeApis(
        pods=_listing(
            pod_body("web-0", containers=[{"name": "app", "restartCount": 0}]),
            pod_body("web-1", containers=[{"name": "app", "restartCount": 0}]),
        )
    )
    per_pod = {
        "web-0": (
            "2026-08-26T10:00:00.000000000Z Traceback (most recent call last):\n"
            '  File "x.py", line 1\n'
            "ValueError: boom\n"
        ),
        "web-1": "2026-08-26T10:00:01.000000000Z still serving\n",
    }
    reader = reader_over(apis)
    original = reader.container_log

    async def _per_pod(namespace, pod, container, **kwargs):
        _ = await original(namespace, pod, container, **kwargs)
        return per_pod[pod]

    reader.container_log = _per_pod  # type: ignore[method-assign]

    lines = [line async for line in reader.logs("conn")]

    assert [line.message for line in lines] == [
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
        "ValueError: boom",
        "still serving",
    ]


async def test_logs_yield_one_line_per_line_with_parsed_timestamps():
    apis = FakeApis(
        pods=_listing(
            pod_body(
                "worker-0",
                containers=[{"name": "worker", "ready": True, "restartCount": 0}],
            )
        ),
        logs=_LOG_TEXT,
    )
    lines = [
        line
        async for line in reader_over(apis).logs(
            "conn", "app=worker", since=timedelta(minutes=5)
        )
    ]

    assert [line.message for line in lines] == [
        "first line",
        "second line",
        "no-timestamp-here",
    ]
    assert all(line.pod == "worker-0" for line in lines)
    assert all(line.container == "worker" for line in lines)
    assert lines[0].timestamp == datetime(2026, 8, 26, 10, 0, 0, 123456, tzinfo=UTC)
    # A line whose prefix does not parse keeps its whole text rather than losing it
    assert lines[2].timestamp is None
    assert apis.kwargs_for("read_namespaced_pod_log")["since_seconds"] == 300


async def test_an_unparsable_timestamp_prefix_keeps_the_whole_line():
    """Losing the message to a formatting surprise is worse than losing the time."""
    apis = FakeApis(
        pods=_listing(
            pod_body("worker-0", containers=[{"name": "worker", "restartCount": 0}])
        ),
        logs="2026-99-99T99:99:99Z something happened\n",
    )
    lines = [line async for line in reader_over(apis).logs("conn")]

    assert len(lines) == 1
    assert lines[0].timestamp is None
    assert lines[0].message == "2026-99-99T99:99:99Z something happened"


async def test_a_sub_second_since_still_asks_for_a_whole_second():
    """``since_seconds`` is an integer; 0 would mean "no bound" to the API."""
    apis = FakeApis(
        pods=_listing(
            pod_body("worker-0", containers=[{"name": "worker", "restartCount": 0}])
        ),
        logs="",
    )
    _ = [
        line
        async for line in reader_over(apis).logs(
            "conn", since=timedelta(milliseconds=200)
        )
    ]

    assert apis.kwargs_for("read_namespaced_pod_log")["since_seconds"] == 1


async def test_logs_of_a_pod_with_no_containers_yield_nothing():
    apis = FakeApis(pods=_listing(pod_body("pending-0", phase="Pending")))

    assert [line async for line in reader_over(apis).logs("conn")] == []


async def test_container_log_asks_for_the_previous_container_and_a_tail():
    apis = FakeApis(logs="crash trace")
    text = await reader_over(apis).container_log(
        "conn", "worker-0", "worker", previous=True, tail_lines=500
    )

    assert text == "crash trace"
    kwargs = apis.kwargs_for("read_namespaced_pod_log")
    assert kwargs["previous"] is True
    assert kwargs["tail_lines"] == 500
    assert kwargs["timestamps"] is True
    assert "since_seconds" not in kwargs


async def test_container_log_defaults_to_the_readers_tail():
    apis = FakeApis(logs="")
    await reader_over(apis, tail_lines=77).container_log("conn", "worker-0", "worker")

    assert apis.kwargs_for("read_namespaced_pod_log")["tail_lines"] == 77


async def test_an_uncapped_tail_omits_the_parameter_entirely():
    """``None`` means "everything retained", which is the API's own default."""
    apis = FakeApis(logs="")
    await reader_over(apis).container_log("conn", "worker-0", "worker", tail_lines=None)

    assert "tail_lines" not in apis.kwargs_for("read_namespaced_pod_log")


async def test_a_container_that_never_restarted_has_no_previous_output():
    """A ``previous`` read on a never-restarted container is a clean 404."""
    apis = FakeApis(logs=ApiException(status=404, reason="Not Found"))
    assert (
        await reader_over(apis).container_log(
            "conn", "worker-0", "worker", previous=True
        )
        == ""
    )


async def test_an_unreadable_log_raises():
    apis = FakeApis(logs=ApiException(status=403, reason="Forbidden"))

    with pytest.raises(ClusterReadFailedError):
        await reader_over(apis).container_log("conn", "worker-0", "worker")


async def test_a_log_read_that_does_not_come_back_as_text_reports_no_output():
    apis = FakeApis(logs=object())
    assert await reader_over(apis).container_log("conn", "worker-0", "worker") == ""


# ---------------------------------------------------------------------------
# HTTP, through the surviving kubectl transport
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, text: str, payload: Any) -> None:
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Any, Any]] = []

    async def request(
        self, method: str, path: str, *, body: Any = None, headers: Any = None
    ) -> _FakeResponse:
        self.calls.append((method, path, body, headers))
        return self.response


def _patched_port_forward(session: _FakeSession) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake(*args: Any, **kwargs: Any):
        _fake.seen = (args, kwargs)  # type: ignore[attr-defined]
        yield session

    return _fake


async def test_http_goes_through_a_port_forward_and_returns_the_status_as_a_value():
    session = _FakeSession(_FakeResponse(503, '{"detail":"down"}', {"detail": "down"}))
    fake = _patched_port_forward(session)

    with patch("application_sdk.testing.harness.cluster.kube.port_forward", fake):
        response = await reader_over(FakeApis(), kube_context="e2e-gcp").http(
            ServiceTarget(namespace="conn", service="handler", port=8000),
            HttpRequest(
                method="POST",
                path="/api/v1/workflows",
                body={"a": 1},
                headers={"X-Test": "1"},
                timeout=timedelta(seconds=7),
            ),
        )

    assert isinstance(response, HttpResponse)
    # A non-2xx is a value, not an exception: the caller's predicate decides
    assert response.status == 503
    assert response.body == {"detail": "down"}
    assert response.text == '{"detail":"down"}'
    assert session.calls == [("POST", "/api/v1/workflows", {"a": 1}, {"X-Test": "1"})]
    # The tunnel is pinned to the same context the reads use. Without this the
    # reader would list pods from one cluster and tunnel into another, both calls
    # succeeding and nothing logged.
    assert fake.seen == (
        ("conn", "handler", 8000),
        {"timeout": 7.0, "kube_context": "e2e-gcp"},
    )


async def test_a_non_json_body_is_reported_as_text_only():
    session = _FakeSession(
        _FakeResponse(502, "<html>bad gateway</html>", ValueError("not json"))
    )

    with patch(
        "application_sdk.testing.harness.cluster.kube.port_forward",
        _patched_port_forward(session),
    ):
        response = await reader_over(FakeApis()).http(
            ServiceTarget(namespace="conn", service="handler", port=8000),
            HttpRequest(method="GET", path="/health"),
        )

    assert response.body is None
    assert response.text == "<html>bad gateway</html>"


# ---------------------------------------------------------------------------
# One ApiClient per thread
# ---------------------------------------------------------------------------


async def test_the_bundle_is_built_once_per_thread_not_once_per_read():
    """The client is not thread-safe, and it is not rebuilt per call either."""
    apis = FakeApis(pods=_listing())
    reader = reader_over(apis)

    await asyncio.gather(*(reader.pods("conn") for _ in range(8)))

    assert len(apis.builds) == len(
        set(apis.builds)
    ), "a thread built more than one bundle"
    assert len(apis.calls) == 8


def test_each_thread_gets_its_own_bundle():
    """Two threads, two bundles — the property ``threading.local`` is for."""
    apis = FakeApis(pods=_listing())
    reader = reader_over(apis)
    seen: list[int] = []

    def _read() -> None:
        asyncio.run(reader.pods("conn"))
        seen.append(threading.get_ident())

    threads = [threading.Thread(target=_read) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 2
    # Each read was offloaded onto the shared pool, so the builds are keyed by
    # pool thread rather than by caller thread — but never more than one each.
    assert len(apis.builds) == len(set(apis.builds))


# ---------------------------------------------------------------------------
# Building from a kubeconfig
# ---------------------------------------------------------------------------


def test_the_readers_context_is_readable_by_whatever_else_reaches_that_cluster():
    """`LogCollector`'s kubectl artefacts and `http()`'s tunnel both need it."""
    assert reader_over(FakeApis(), kube_context="e2e-gcp").kube_context == "e2e-gcp"
    assert reader_over(FakeApis()).kube_context is None


def test_a_missing_extra_names_the_extra_rather_than_a_module():
    with patch.dict(sys.modules, {"kubernetes": None}):
        with pytest.raises(KubernetesExtraMissingError) as raised:
            kubeconfig_apis()

    assert raised.value.extra == "harness"
    assert "atlan-application-sdk[harness]" in raised.value.message


def test_an_unusable_kubeconfig_names_the_context():
    with patch(
        "kubernetes.config.load_kube_config",
        side_effect=RuntimeError("no such context"),
    ):
        with pytest.raises(KubeconfigUnavailableError) as raised:
            kubeconfig_apis(kube_context="e2e-gcp")

    assert raised.value.kube_context == "e2e-gcp"
    assert "e2e-gcp" in raised.value.message


def test_the_kubeconfig_factory_loads_into_a_fresh_configuration():
    """Never into the client's process-wide default: pool threads would collide."""
    with patch("kubernetes.config.load_kube_config") as load:
        bundle = kubeconfig_apis(kube_context="e2e-gcp")

    assert bundle.kube_context == "e2e-gcp"
    assert load.call_args.kwargs["context"] == "e2e-gcp"
    assert load.call_args.kwargs["client_configuration"] is not None
    # Every read surface the backend declares is present and callable
    assert callable(bundle.core.list_namespaced_pod)
    assert callable(bundle.apps.list_namespaced_deployment)
    assert callable(bundle.custom.list_namespaced_custom_object)
    assert callable(bundle.crds.read_custom_resource_definition)
    assert bundle.sanitize({"a": 1}) == {"a": 1}


def test_the_default_reader_builds_from_the_ambient_kubeconfig():
    """No ``apis`` factory means the kubeconfig one, carrying the context."""
    reader = KubernetesReader(kube_context="e2e-gcp")

    with patch("kubernetes.config.load_kube_config") as load:
        bundle = reader._build()

    assert bundle.kube_context == "e2e-gcp"
    assert load.call_args.kwargs["context"] == "e2e-gcp"
