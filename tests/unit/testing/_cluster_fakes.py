"""Test doubles for the typed cluster backend (FND-241).

The doubles return **plain dicts with the manifest's own camelCase keys** — which
is exactly what ``KubernetesApis.sanitize`` produces from a real model, so the
code path under test is the production one rather than a mock-shaped variant of
it. ``sanitize`` on a dict is the identity, so the double simply omits it.

The narrowing under test needs a *real* ``ApiException``: the backend converts
only the client's own error type and lets everything else through, and a stub
exception would let a test pass against a narrowing that had stopped working.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

pytest.importorskip("kubernetes", reason="requires the `harness` extra")

from kubernetes.client.exceptions import ApiException  # noqa: E402

from application_sdk.testing.harness.cluster.kube import (  # noqa: E402
    KubernetesApis,
    KubernetesReader,
)

__all__ = [
    "ApiException",
    "FakeApis",
    "reader_over",
]

#: A ``Raise`` in a fake's script means "the API server answered with this".
Answer = object | BaseException


def _answer(value: Answer) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


class _RecordingApi:
    """Base for the fakes: every call is recorded as ``(method, args, kwargs)``."""

    def __init__(
        self, calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]
    ) -> None:
        self.calls = calls

    def _record(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.calls.append((method, args, kwargs))


class _FakeCore(_RecordingApi):
    def __init__(
        self,
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]],
        pods: Answer,
        logs: Answer,
    ) -> None:
        super().__init__(calls)
        self._pods = pods
        self._logs = logs

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> object:
        self._record("list_namespaced_pod", (namespace,), kwargs)
        return _answer(self._pods)

    def read_namespaced_pod_log(
        self, name: str, namespace: str, **kwargs: Any
    ) -> object:
        self._record("read_namespaced_pod_log", (name, namespace), kwargs)
        return _answer(self._logs)


class _FakeApps(_RecordingApi):
    def __init__(
        self,
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]],
        deployments: Answer,
    ) -> None:
        super().__init__(calls)
        self._deployments = deployments

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> object:
        self._record("list_namespaced_deployment", (namespace,), kwargs)
        return _answer(self._deployments)


class _FakeCustom(_RecordingApi):
    def __init__(
        self,
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]],
        resources: Answer,
    ) -> None:
        super().__init__(calls)
        self._resources = resources

    def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **kwargs: Any
    ) -> object:
        self._record(
            "list_namespaced_custom_object", (group, version, namespace, plural), kwargs
        )
        return _answer(self._resources)

    def list_cluster_custom_object(
        self, group: str, version: str, plural: str, **kwargs: Any
    ) -> object:
        self._record("list_cluster_custom_object", (group, version, plural), kwargs)
        return _answer(self._resources)

    def get_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        **kwargs: Any,
    ) -> object:
        self._record(
            "get_namespaced_custom_object",
            (group, version, namespace, plural, name),
            kwargs,
        )
        return _answer(self._resources)

    def get_cluster_custom_object(
        self, group: str, version: str, plural: str, name: str, **kwargs: Any
    ) -> object:
        self._record(
            "get_cluster_custom_object", (group, version, plural, name), kwargs
        )
        return _answer(self._resources)


class _FakeCrds(_RecordingApi):
    def __init__(
        self, calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]], crd: Answer
    ) -> None:
        super().__init__(calls)
        self._crd = crd

    def read_custom_resource_definition(self, name: str, **kwargs: Any) -> object:
        self._record("read_custom_resource_definition", (name,), kwargs)
        return _answer(self._crd)


class FakeApis:
    """Scripted answers for one reader, plus a record of what was asked.

    Args:
        pods: What ``list_namespaced_pod`` answers with. A ``BaseException`` is
            raised instead of returned.
        deployments: What ``list_namespaced_deployment`` answers with.
        resources: What every ``CustomObjectsApi`` verb answers with.
        crd: What ``read_custom_resource_definition`` answers with.
        logs: What ``read_namespaced_pod_log`` answers with.
    """

    def __init__(
        self,
        *,
        pods: Answer = None,
        deployments: Answer = None,
        resources: Answer = None,
        crd: Answer = None,
        logs: Answer = "",
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.builds: list[int] = []
        self._pods = pods
        self._deployments = deployments
        self._resources = resources
        self._crd = crd
        self._logs = logs

    def __call__(self) -> KubernetesApis:
        """Build one thread's bundle, recording which thread asked."""
        self.builds.append(threading.get_ident())
        return KubernetesApis(
            core=_FakeCore(self.calls, self._pods, self._logs),
            apps=_FakeApps(self.calls, self._deployments),
            custom=_FakeCustom(self.calls, self._resources),
            crds=_FakeCrds(self.calls, self._crd),
            # A dict is already sanitized; the identity keeps the double honest
            # about producing what the real conversion produces.
            sanitize=lambda obj: obj,
            kube_context="fake-context",
        )

    def kwargs_for(self, method: str) -> dict[str, Any]:
        """The keyword arguments the reader passed to *method*, first call."""
        for name, _args, kwargs in self.calls:
            if name == method:
                return kwargs
        raise AssertionError(f"{method} was never called; saw {self.methods()}")

    def args_for(self, method: str) -> tuple[Any, ...]:
        """The positional arguments the reader passed to *method*, first call."""
        for name, args, _kwargs in self.calls:
            if name == method:
                return args
        raise AssertionError(f"{method} was never called; saw {self.methods()}")

    def methods(self) -> list[str]:
        """Every method called, in order."""
        return [name for name, _args, _kwargs in self.calls]


def reader_over(apis: Callable[[], KubernetesApis], **kwargs: Any) -> KubernetesReader:
    """A reader wired to *apis* instead of to a real kubeconfig."""
    return KubernetesReader(apis=apis, **kwargs)


def pod_body(
    name: str,
    *,
    phase: str = "Running",
    containers: Sequence[Mapping[str, Any]] | None = None,
    namespace: str | None = None,
    node: str | None = "node-a",
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One pod as ``sanitize_for_serialization`` would render it."""
    metadata: dict[str, Any] = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    if labels is not None:
        metadata["labels"] = dict(labels)
    status: dict[str, Any] = {"phase": phase}
    if containers is not None:
        status["containerStatuses"] = [dict(c) for c in containers]
    return {
        "metadata": metadata,
        "spec": {"nodeName": node} if node is not None else {},
        "status": status,
    }


def deployment_body(
    name: str,
    *,
    desired: int | None = 3,
    ready: int | None = 1,
    updated: int | None = 2,
    namespace: str | None = None,
) -> dict[str, Any]:
    """One Deployment as ``sanitize_for_serialization`` would render it."""
    metadata: dict[str, Any] = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    spec = {} if desired is None else {"replicas": desired}
    status: dict[str, Any] = {}
    if ready is not None:
        status["readyReplicas"] = ready
    if updated is not None:
        status["updatedReplicas"] = updated
    return {"metadata": metadata, "spec": spec, "status": status}
