"""Activity-aware ``karpenter.sh/do-not-disrupt`` on the worker's own pod.

Karpenter will not consolidate a node while any pod on it carries
``karpenter.sh/do-not-disrupt: "true"``, and it re-reads that annotation on every
decision pass. A static annotation set at pod creation therefore pins the node for
the pod's whole life, including the long stretches where the worker is idle.

This interceptor keeps the annotation in step with real work instead: it is set
before the first in-flight activity starts and removed once none have run for
``idle_seconds``. If a patch fails the activity still runs; the pod is then only as
protected as a pod without the annotation, and an eviction drains it through the
normal graceful-shutdown path.

The guard only manages pods the chart asked to protect: if the pod was created
without the annotation, the guard does nothing. Its first patch also writes a marker
annotation, so a container restart after the annotation was removed still knows the
pod is managed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

DO_NOT_DISRUPT_ANNOTATION = "karpenter.sh/do-not-disrupt"
MANAGED_ANNOTATION = "application-sdk.atlan.com/dynamic-do-not-disrupt"
_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_PATCH_TIMEOUT_SECONDS = 5.0


class PodAnnotationPatcher:
    """Sets or removes ``karpenter.sh/do-not-disrupt`` on one pod via the API server."""

    def __init__(
        self,
        pod: str,
        namespace: str,
        *,
        api_url: str = "https://kubernetes.default.svc",
        service_account_dir: Path = _SERVICE_ACCOUNT_DIR,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{api_url}/api/v1/namespaces/{namespace}/pods/{pod}"
        self._token_path = service_account_dir / "token"
        ca_path = service_account_dir / "ca.crt"
        self._verify: Any = str(ca_path) if ca_path.exists() else True
        self._transport = transport

    @classmethod
    def from_environment(cls) -> PodAnnotationPatcher | None:
        """Build a patcher for the current pod, or ``None`` outside Kubernetes."""
        namespace_file = _SERVICE_ACCOUNT_DIR / "namespace"
        pod = os.environ.get("K8S_POD_NAME") or os.environ.get("HOSTNAME")
        if not pod or not namespace_file.exists():
            return None
        return cls(pod, namespace_file.read_text().strip())

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify,
            timeout=_PATCH_TIMEOUT_SECONDS,
            transport=self._transport,
        )

    def _auth(self) -> dict[str, str]:
        # Projected service-account tokens rotate, so read it on every call.
        return {"Authorization": f"Bearer {self._token_path.read_text().strip()}"}

    async def get_annotations(self) -> dict[str, str]:
        async with self._client() as client:
            response = await client.get(self._url, headers=self._auth())
            response.raise_for_status()
            return response.json().get("metadata", {}).get("annotations") or {}

    async def set(self, protected: bool) -> None:
        body = {
            "metadata": {
                "annotations": {
                    DO_NOT_DISRUPT_ANNOTATION: "true" if protected else None,
                    MANAGED_ANNOTATION: "true",
                }
            }
        }
        async with self._client() as client:
            response = await client.patch(
                self._url,
                content=json.dumps(body),
                headers={
                    **self._auth(),
                    "Content-Type": "application/merge-patch+json",
                },
            )
            response.raise_for_status()


class DisruptionGuard:
    """Counts in-flight activities and keeps the pod annotation in step with them."""

    def __init__(
        self,
        set_protected: Callable[[bool], Awaitable[None]],
        *,
        idle_seconds: float = 60.0,
        read_annotations: Callable[[], Awaitable[dict[str, str]]] | None = None,
    ) -> None:
        self._set_protected = set_protected
        self._idle_seconds = idle_seconds
        self._read_annotations = read_annotations
        # Decided once, from the pod's own annotations; without a reader, always managed.
        self._managed: bool | None = None if read_annotations else True
        self._running = 0
        # Assume a creation-time static annotation may be present until the first
        # release confirms otherwise.
        self._protected = True
        self._lock = asyncio.Lock()
        self._release_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> int:
        return self._running

    @property
    def protected(self) -> bool:
        return self._protected

    @property
    def managed(self) -> bool | None:
        return self._managed

    def start(self) -> None:
        """Release a creation-time annotation once the worker has been idle."""
        asyncio.get_running_loop().create_task(self._start())

    async def _start(self) -> None:
        async with self._lock:
            if await self._is_managed():
                self._schedule_release()

    async def _is_managed(self) -> bool:
        if self._managed is None:
            try:
                annotations = await self._read_annotations()  # type: ignore[misc]
            # conformance: ignore[E004] best-effort read; manage rather than leave a chart annotation pinned
            except Exception:
                logger.warning(
                    "Could not read this pod's annotations; managing %s anyway",
                    DO_NOT_DISRUPT_ANNOTATION,
                    exc_info=True,
                )
                self._managed = True
            else:
                self._managed = (
                    annotations.get(DO_NOT_DISRUPT_ANNOTATION) == "true"
                    or MANAGED_ANNOTATION in annotations
                )
                if not self._managed:
                    logger.info(
                        "This pod was created without %s; dynamic do-not-disrupt does nothing",
                        DO_NOT_DISRUPT_ANNOTATION,
                    )
        return self._managed

    async def activity_started(self) -> None:
        async with self._lock:
            if not await self._is_managed():
                return
            self._running += 1
            self._cancel_release()
            if not self._protected:
                await self._apply(True)

    async def activity_finished(self) -> None:
        async with self._lock:
            if not self._managed:
                return
            self._running = max(0, self._running - 1)
            if self._running == 0:
                self._schedule_release()

    def _cancel_release(self) -> None:
        if self._release_task is not None and not self._release_task.done():
            self._release_task.cancel()
        self._release_task = None

    def _schedule_release(self) -> None:
        self._cancel_release()
        self._release_task = asyncio.get_running_loop().create_task(
            self._release_after_idle()
        )

    async def _release_after_idle(self) -> None:
        await asyncio.sleep(self._idle_seconds)
        async with self._lock:
            if self._running == 0 and self._protected:
                await self._apply(False)

    async def _apply(self, protected: bool) -> None:
        try:
            await self._set_protected(protected)
        # conformance: ignore[E004] best-effort annotation; an activity must never fail on it
        except Exception:
            logger.warning(
                "Could not %s %s on this pod; %s",
                "set" if protected else "remove",
                DO_NOT_DISRUPT_ANNOTATION,
                "the activity runs unprotected and would drain on eviction"
                if protected
                else "the node stays pinned until the next release",
                exc_info=True,
            )
            return
        self._protected = protected
        logger.info(
            "%s %s (in-flight activities: %d)",
            "Set" if protected else "Removed",
            DO_NOT_DISRUPT_ANNOTATION,
            self._running,
        )


_process_guard: DisruptionGuard | None = None


def process_guard(idle_seconds: float) -> DisruptionGuard | None:
    """Return this process's guard, creating it on first use.

    The annotation belongs to the pod, so every worker in the process must share one
    guard: with a guard per worker, an idle worker would release the annotation while
    another worker in the same pod is still running an activity. Returns ``None``
    outside Kubernetes.
    """
    global _process_guard
    if _process_guard is None:
        patcher = PodAnnotationPatcher.from_environment()
        if patcher is None:
            return None
        _process_guard = DisruptionGuard(
            patcher.set,
            idle_seconds=idle_seconds,
            read_annotations=patcher.get_annotations,
        )
    return _process_guard


class _DisruptionGuardActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, guard: DisruptionGuard
    ) -> None:
        super().__init__(next)
        self._guard = guard

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        await self._guard.activity_started()
        try:
            return await self.next.execute_activity(input)
        finally:
            await self._guard.activity_finished()


class DisruptionGuardInterceptor(Interceptor):
    """Wraps every activity so the pod is protected exactly while work is in flight.

    SDK-internal: ``create_worker`` adds it when
    ``APPLICATION_SDK_ENABLE_DYNAMIC_DO_NOT_DISRUPT`` is on and the worker runs in a
    pod. The pod's service account needs ``get`` and ``patch`` on pods in its
    namespace.
    """

    def __init__(self, guard: DisruptionGuard) -> None:
        self._guard = guard

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _DisruptionGuardActivityInboundInterceptor(next, self._guard)
