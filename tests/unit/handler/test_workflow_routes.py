"""The workflow lifecycle routes, registered on a bare app through their source.

These build only ``register_workflow_routes`` over a fake Temporal client, not
the whole handler service, so they pin the module's own contract: the 503
guard, the ownership 404, the Temporal error ladder and the result envelopes.
The service-level tests (``test_service_workflow_lifecycle.py``,
``test_service_start.py``) cover the same routes as the worker wires them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode

from application_sdk.handler import workflow_routes
from application_sdk.handler.service import WorkflowClientConfig
from application_sdk.handler.workflow_routes import (
    WorkflowRoutesSource,
    register_workflow_routes,
)

_QUEUE = "routes-test-queue"
_TYPE = "routes-test-workflow"


class _App:
    _app_name = "routes-test"


def _config(*, configured: bool = True) -> WorkflowClientConfig:
    return WorkflowClientConfig(
        host="localhost:7233" if configured else "",
        task_queue=_QUEUE,
        app_name="routes-test",
        app_class=_App,
    )


@pytest.fixture(autouse=True)
def _own_workflow_type(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``_TYPE`` a workflow type this app registers, without an app registry."""
    output = SimpleNamespace(output_type=None)
    monkeypatch.setattr(
        workflow_routes, "_registered_workflow_types", lambda _config: {_TYPE: output}
    )
    yield


def _desc(
    status: WorkflowExecutionStatus | None,
    *,
    task_queue: str = _QUEUE,
    workflow_type: str = _TYPE,
    execution_time: datetime | None = None,
    close_time: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        task_queue=task_queue,
        workflow_type=workflow_type,
        run_id="run-1",
        execution_time=execution_time,
        close_time=close_time,
    )


def _temporal(desc: Any = None, *, result: Any = None) -> MagicMock:
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=desc)
    handle.terminate = AsyncMock()
    handle.result = AsyncMock(return_value=result)
    client = MagicMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.handle = handle
    return client


def _routes(
    client: MagicMock, *, configured: bool = True
) -> tuple[TestClient, AsyncMock]:
    get_client = AsyncMock(return_value=client)
    app = FastAPI()
    register_workflow_routes(
        app,
        WorkflowRoutesSource(
            app_name="routes-test",
            workflow_config=_config(configured=configured),
            temporal_client=get_client,
            normalize=lambda body: body,
        ),
    )
    return TestClient(app, raise_server_exceptions=False), get_client


class TestNotConfigured:
    @pytest.mark.parametrize(
        ("method", "path", "detail"),
        [
            (
                "post",
                "/workflows/v1/start",
                "Workflow execution not configured. Set ATLAN_TEMPORAL_HOST.",
            ),
            (
                "post",
                "/workflows/v1/stop/wf/run-1",
                "Workflow execution not configured.",
            ),
            ("get", "/workflows/v1/result/wf", "Workflow execution not configured."),
            (
                "get",
                "/workflows/v1/status/wf/run-1",
                "Workflow execution not configured.",
            ),
        ],
    )
    def test_every_route_answers_503_without_touching_temporal(
        self, method: str, path: str, detail: str
    ) -> None:
        http, get_client = _routes(_temporal(), configured=False)

        response = getattr(http, method)(path)

        assert response.status_code == 503
        assert response.json()["detail"] == detail
        get_client.assert_not_awaited()


class TestTemporalErrorLadder:
    """Not-found is 404 on every route; anything else is that route's own 500."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/workflows/v1/stop/wf/run-1"),
            ("get", "/workflows/v1/result/wf"),
            ("get", "/workflows/v1/status/wf/run-1"),
        ],
    )
    def test_rpc_not_found_is_404(self, method: str, path: str) -> None:
        client = _temporal()
        client.handle.describe.side_effect = RPCError(
            "missing", RPCStatusCode.NOT_FOUND, b""
        )
        http, _ = _routes(client)

        response = getattr(http, method)(path)

        assert response.status_code == 404
        assert response.json()["detail"] == "Workflow not found: wf"

    @pytest.mark.parametrize(
        ("method", "path", "detail"),
        [
            ("post", "/workflows/v1/stop/wf/run-1", "Failed to stop workflow"),
            ("get", "/workflows/v1/result/wf", "Failed to get workflow result"),
            ("get", "/workflows/v1/status/wf/run-1", "Failed to get workflow status"),
        ],
    )
    def test_any_other_failure_is_a_sanitized_500(
        self, method: str, path: str, detail: str
    ) -> None:
        client = _temporal()
        client.handle.describe.side_effect = RPCError(
            "frontend exploded: secret-host:7233", RPCStatusCode.UNAVAILABLE, b""
        )
        http, _ = _routes(client)

        response = getattr(http, method)(path)

        assert response.status_code == 500
        assert response.json()["detail"] == detail

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/workflows/v1/stop/wf/run-1"),
            ("get", "/workflows/v1/result/wf"),
            ("get", "/workflows/v1/status/wf/run-1"),
        ],
    )
    def test_another_apps_run_is_404_and_never_acted_on(
        self, method: str, path: str
    ) -> None:
        client = _temporal(
            _desc(WorkflowExecutionStatus.COMPLETED, task_queue="other-queue")
        )
        http, _ = _routes(client)

        response = getattr(http, method)(path)

        assert response.status_code == 404
        client.handle.terminate.assert_not_awaited()
        client.handle.result.assert_not_awaited()


class TestResult:
    @pytest.mark.parametrize(
        ("status", "wait"),
        [
            (WorkflowExecutionStatus.COMPLETED, False),
            (WorkflowExecutionStatus.RUNNING, True),
        ],
    )
    def test_completed_and_wait_answer_the_same_result(
        self, status: WorkflowExecutionStatus, wait: bool
    ) -> None:
        client = _temporal(_desc(status), result={"rows": 3})
        http, _ = _routes(client)

        response = http.get(f"/workflows/v1/result/wf?wait={str(wait).lower()}")

        assert response.status_code == 200
        body = response.json()
        assert body["message"] == "Workflow completed"
        assert body["data"] == {
            "status": "completed",
            "workflow_id": "wf",
            "result": {"rows": 3},
        }
        # Read pinned to the run whose ownership was checked (FND-5215).
        assert client.get_workflow_handle.call_args.kwargs["run_id"] == "run-1"

    def test_run_failure_answers_failed_envelope(self) -> None:
        client = _temporal(_desc(WorkflowExecutionStatus.COMPLETED))
        failure = WorkflowFailureError(cause=RuntimeError("boom"))
        client.handle.result.side_effect = failure
        http, _ = _routes(client)

        body = http.get("/workflows/v1/result/wf").json()

        assert body["success"] is False
        assert body["data"] == {
            "status": "failed",
            "workflow_id": "wf",
            "error": "Workflow execution failed.",
        }

    def test_undecodable_result_names_the_error_type(self) -> None:
        client = _temporal(_desc(WorkflowExecutionStatus.COMPLETED))
        client.handle.result.side_effect = ValueError("bad payload")
        http, _ = _routes(client)

        body = http.get("/workflows/v1/result/wf").json()

        assert body["success"] is False
        assert body["data"]["status"] == "result_decode_failed"
        assert body["data"]["error_type"] == "ValueError"

    @pytest.mark.parametrize(
        ("status", "expected", "success"),
        [
            (WorkflowExecutionStatus.RUNNING, "running", True),
            (WorkflowExecutionStatus.TERMINATED, "failed", False),
            (WorkflowExecutionStatus.TIMED_OUT, "failed", False),
            (WorkflowExecutionStatus.CONTINUED_AS_NEW, "continued_as_new", True),
            (None, "unknown", True),
        ],
    )
    def test_unfinished_run_answers_its_state(
        self, status: WorkflowExecutionStatus | None, expected: str, success: bool
    ) -> None:
        client = _temporal(_desc(status))
        http, _ = _routes(client)

        body = http.get("/workflows/v1/result/wf").json()

        assert body["success"] is success
        assert body["data"]["status"] == expected
        client.handle.result.assert_not_awaited()


class TestStopAndStatus:
    def test_stop_terminates_the_owned_run(self) -> None:
        client = _temporal(_desc(WorkflowExecutionStatus.RUNNING))
        http, _ = _routes(client)

        response = http.post("/workflows/v1/stop/wf/run-1")

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "Workflow terminated successfully",
        }
        client.handle.terminate.assert_awaited_once()

    def test_status_reports_closed_run_duration(self) -> None:
        started = datetime(2026, 1, 1, tzinfo=UTC)
        client = _temporal(
            _desc(
                WorkflowExecutionStatus.COMPLETED,
                execution_time=started,
                close_time=started + timedelta(seconds=90),
            )
        )
        http, _ = _routes(client)

        body = http.get("/workflows/v1/status/wf/run-1").json()

        assert body["data"] == {
            "workflow_id": "wf",
            "run_id": "run-1",
            "status": "COMPLETED",
            "execution_duration_seconds": 90,
        }

    def test_client_is_resolved_per_request(self) -> None:
        client = _temporal(_desc(WorkflowExecutionStatus.RUNNING))
        http, get_client = _routes(client)
        get_client.assert_not_awaited()

        http.get("/workflows/v1/status/wf/run-1")
        http.get("/workflows/v1/status/wf/run-1")

        assert get_client.await_count == 2
