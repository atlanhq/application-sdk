"""Unit tests for the activity-aware do-not-disrupt guard."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from application_sdk.execution._temporal.interceptors import disruption_guard
from application_sdk.execution._temporal.interceptors.disruption_guard import (
    DO_NOT_DISRUPT_ANNOTATION,
    MANAGED_ANNOTATION,
    DisruptionGuard,
    DisruptionGuardInterceptor,
    PodAnnotationPatcher,
)

IDLE = 0.05


@dataclass
class _Input:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


class _Recorder:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[bool] = []
        self._fail_first = fail_first

    async def __call__(self, protected: bool) -> None:
        if self._fail_first:
            self._fail_first -= 1
            raise httpx.HTTPError("api server unavailable")
        self.calls.append(protected)


def _inbound(guard: DisruptionGuard, body):
    next_inbound = MagicMock()
    next_inbound.execute_activity = body
    return DisruptionGuardInterceptor(guard).intercept_activity(next_inbound)


async def _released(guard: DisruptionGuard) -> None:
    await asyncio.sleep(IDLE * 4)


@pytest.mark.asyncio
async def test_protects_before_the_activity_body_runs() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE)
    await guard._apply(False)  # an idle pod with no annotation
    seen_during_body: list[bool] = []

    async def body(_input):
        seen_during_body.append(guard.protected)
        return "done"

    assert await _inbound(guard, body).execute_activity(_Input()) == "done"

    assert seen_during_body == [True]
    assert recorder.calls[:2] == [False, True]


@pytest.mark.asyncio
async def test_overlapping_activities_release_only_after_the_last_one() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE)
    await guard._apply(False)
    gate = asyncio.Event()

    async def body(_input):
        await gate.wait()

    first = asyncio.create_task(_inbound(guard, body).execute_activity(_Input()))
    second = asyncio.create_task(_inbound(guard, body).execute_activity(_Input()))
    await asyncio.sleep(0)
    assert guard.running == 2 and guard.protected

    gate.set()
    await asyncio.gather(first, second)
    await _released(guard)

    assert recorder.calls == [False, True, False]
    assert not guard.protected


@pytest.mark.asyncio
async def test_activity_inside_the_idle_window_keeps_protection() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE * 4)
    await guard._apply(False)

    async def body(_input):
        return None

    await _inbound(guard, body).execute_activity(_Input())
    await asyncio.sleep(IDLE)  # well inside the idle window
    await _inbound(guard, body).execute_activity(_Input())

    assert recorder.calls == [False, True]
    assert guard.protected


@pytest.mark.asyncio
async def test_start_releases_a_creation_time_annotation_once_idle() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE)

    guard.start()
    await _released(guard)

    assert recorder.calls == [False]
    assert not guard.protected


@pytest.mark.asyncio
async def test_failed_patch_never_fails_the_activity_and_is_retried() -> None:
    recorder = _Recorder(fail_first=1)
    guard = DisruptionGuard(recorder, idle_seconds=IDLE * 4)
    guard._protected = False

    async def body(_input):
        return "done"

    assert await _inbound(guard, body).execute_activity(_Input()) == "done"
    assert recorder.calls == [] and not guard.protected

    await _inbound(guard, body).execute_activity(_Input())
    assert recorder.calls == [True]


@pytest.mark.asyncio
async def test_failed_activity_still_releases() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE)
    await guard._apply(False)

    async def body(_input):
        raise RuntimeError("activity failed")

    with pytest.raises(RuntimeError):
        await _inbound(guard, body).execute_activity(_Input())
    await _released(guard)

    assert guard.running == 0
    assert recorder.calls == [False, True, False]


@pytest.mark.asyncio
async def test_two_workers_sharing_a_guard_keep_the_pod_protected() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE)
    await guard._apply(False)
    long_running = asyncio.Event()

    async def slow(_input):
        await long_running.wait()

    async def fast(_input):
        return None

    first_worker = asyncio.create_task(_inbound(guard, slow).execute_activity(_Input()))
    await asyncio.sleep(0)
    await _inbound(guard, fast).execute_activity(_Input())
    await _released(guard)

    assert guard.protected and recorder.calls == [False, True]

    long_running.set()
    await first_worker
    await _released(guard)
    assert recorder.calls == [False, True, False]


def _reader(annotations=None, error=None):
    async def read():
        if error:
            raise error
        return annotations or {}

    return read


async def _run_once(guard: DisruptionGuard) -> None:
    async def body(_input):
        return None

    guard.start()
    await asyncio.sleep(0)
    await _inbound(guard, body).execute_activity(_Input())
    await _released(guard)


@pytest.mark.asyncio
async def test_pod_created_without_the_annotation_is_left_alone() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(recorder, idle_seconds=IDLE, read_annotations=_reader({}))
    await _run_once(guard)
    assert guard.managed is False and recorder.calls == [] and guard.running == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "annotations",
    [{DO_NOT_DISRUPT_ANNOTATION: "true"}, {MANAGED_ANNOTATION: "true"}],
    ids=["chart-annotation", "marker-after-restart"],
)
async def test_pod_the_chart_protected_is_managed(annotations) -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(
        recorder, idle_seconds=IDLE, read_annotations=_reader(annotations)
    )
    await _run_once(guard)
    assert guard.managed is True and recorder.calls[-1] is False


@pytest.mark.asyncio
async def test_unreadable_annotations_are_managed() -> None:
    recorder = _Recorder()
    guard = DisruptionGuard(
        recorder,
        idle_seconds=IDLE,
        read_annotations=_reader(error=httpx.HTTPError("forbidden")),
    )
    await _run_once(guard)
    assert guard.managed is True and recorder.calls[-1] is False


@pytest.mark.asyncio
async def test_patcher_reads_its_own_annotations(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200, json={"metadata": {"annotations": {MANAGED_ANNOTATION: "true"}}}
        )

    patcher = PodAnnotationPatcher(
        "worker-abc",
        "app-ns",
        api_url="https://api.test",
        service_account_dir=_service_account(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    assert await patcher.get_annotations() == {MANAGED_ANNOTATION: "true"}


def test_process_guard_is_shared_and_absent_outside_a_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(disruption_guard, "_process_guard", None)
    monkeypatch.setattr(
        disruption_guard.PodAnnotationPatcher,
        "from_environment",
        classmethod(lambda cls: None),
    )
    assert disruption_guard.process_guard(60) is None

    monkeypatch.setattr(
        disruption_guard.PodAnnotationPatcher,
        "from_environment",
        classmethod(lambda cls: MagicMock()),
    )
    first = disruption_guard.process_guard(60)
    assert first is not None and disruption_guard.process_guard(60) is first


def _service_account(tmp_path: Path) -> Path:
    (tmp_path / "token").write_text("token-1\n")
    return tmp_path


@pytest.mark.asyncio
async def test_patcher_sends_a_merge_patch_for_its_own_pod(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    patcher = PodAnnotationPatcher(
        "worker-abc",
        "app-ns",
        api_url="https://api.test",
        service_account_dir=_service_account(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    await patcher.set(True)
    await patcher.set(False)

    assert [r.method for r in requests] == ["PATCH", "PATCH"]
    assert (
        str(requests[0].url)
        == "https://api.test/api/v1/namespaces/app-ns/pods/worker-abc"
    )
    assert requests[0].headers["Content-Type"] == "application/merge-patch+json"
    assert requests[0].headers["Authorization"] == "Bearer token-1"
    annotations = [json.loads(r.content)["metadata"]["annotations"] for r in requests]
    assert annotations == [
        {DO_NOT_DISRUPT_ANNOTATION: "true", MANAGED_ANNOTATION: "true"},
        {DO_NOT_DISRUPT_ANNOTATION: None, MANAGED_ANNOTATION: "true"},
    ]


@pytest.mark.asyncio
async def test_patcher_raises_when_the_api_refuses(tmp_path: Path) -> None:
    patcher = PodAnnotationPatcher(
        "worker-abc",
        "app-ns",
        api_url="https://api.test",
        service_account_dir=_service_account(tmp_path),
        transport=httpx.MockTransport(lambda _r: httpx.Response(403, json={})),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await patcher.set(True)


def test_from_environment_outside_a_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "application_sdk.execution._temporal.interceptors.disruption_guard._SERVICE_ACCOUNT_DIR",
        tmp_path / "missing",
    )
    monkeypatch.setenv("K8S_POD_NAME", "worker-abc")
    assert PodAnnotationPatcher.from_environment() is None
