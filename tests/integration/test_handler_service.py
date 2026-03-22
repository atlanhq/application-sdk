"""G6: Integration tests for the Handler HTTP layer.

Verifies the handler service works end-to-end with real (in-memory)
infrastructure, local obstore for file uploads, and a real Temporal dev
server for workflow lifecycle endpoints.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.base import DefaultHandler, Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)

# ---------------------------------------------------------------------------
# Global reset fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_service_globals():
    """Reset handler service module globals and infrastructure context before/after each test."""
    from application_sdk.handler import service as svc
    from application_sdk.infrastructure.context import _infrastructure_ctx

    svc._temporal_client = None
    svc._workflow_config = svc.WorkflowClientConfig()
    svc._state_store = None
    svc._storage = None
    _infrastructure_ctx.set(None)

    yield

    svc._temporal_client = None
    svc._workflow_config = svc.WorkflowClientConfig()
    svc._state_store = None
    svc._storage = None
    _infrastructure_ctx.set(None)


# ---------------------------------------------------------------------------
# Infrastructure fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_infra():
    """Set up InMemory infrastructure with seeded secret store."""
    from application_sdk.infrastructure.context import (
        InfrastructureContext,
        set_infrastructure,
    )
    from application_sdk.testing.mocks import MockSecretStore, MockStateStore

    state = MockStateStore()
    secrets = MockSecretStore(secrets={"my-secret": "secret-value-abc"})
    set_infrastructure(InfrastructureContext(state_store=state, secret_store=secrets))
    yield state, secrets


# ---------------------------------------------------------------------------
# Handler Context & Infrastructure (tests 1–4)
# ---------------------------------------------------------------------------


class _ContextCapturingHandler(Handler):
    """Handler that records context details during test_auth."""

    def __init__(self) -> None:
        self.captured_app_name: str = ""
        self.captured_request_ids: list[UUID] = []
        self.captured_credential: str | None = None
        self.captured_secret: str | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        ctx = self.context
        self.captured_app_name = ctx.app_name
        self.captured_request_ids.append(ctx.request_id)
        self.captured_credential = ctx.get_credential("api_key")
        if ctx._secret_store is not None:
            self.captured_secret = await ctx.get_secret("my-secret")
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[], total_count=0)


class _ErrorHandler(Handler):
    """Handler that raises HandlerError with a custom http_status."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("forbidden", http_status=403)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[], total_count=0)


@pytest.fixture
def context_handler():
    """Create a handler service with the context-capturing handler."""
    from application_sdk.handler.service import create_app_handler_service

    handler = _ContextCapturingHandler()
    app = create_app_handler_service(handler, app_name="ctx-test-app")
    return app, handler


@pytest.fixture
async def context_client(context_handler):
    app, handler = context_handler
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, handler


@pytest.mark.integration
async def test_handler_context_wiring(context_client):
    """G6.1: HandlerContext has correct app_name, UUID request_id, and credentials."""
    client, handler = context_client

    resp = await client.post(
        "/workflows/v1/auth",
        json={"credentials": [{"key": "api_key", "value": "secret123"}]},
    )

    assert resp.status_code == 200
    assert handler.captured_app_name == "ctx-test-app"
    assert len(handler.captured_request_ids) == 1
    # Verify it's a valid UUID
    UUID(str(handler.captured_request_ids[0]))
    assert handler.captured_credential == "secret123"


@pytest.mark.integration
async def test_handler_context_secret_store_access(mock_infra, context_client):
    """G6.2: HandlerContext.get_secret() reads from the InfrastructureContext secret store."""
    client, handler = context_client

    resp = await client.post(
        "/workflows/v1/auth",
        json={"credentials": []},
    )

    assert resp.status_code == 200
    assert handler.captured_secret == "secret-value-abc"


@pytest.mark.integration
async def test_handler_context_cleanup_between_requests(context_client):
    """G6.3: Each request gets an independent request_id; _context is None between requests."""
    client, handler = context_client

    body = {"credentials": []}
    resp1 = await client.post("/workflows/v1/auth", json=body)
    assert resp1.status_code == 200
    # After the first request, context should be cleared
    assert handler._context is None

    resp2 = await client.post("/workflows/v1/auth", json=body)
    assert resp2.status_code == 200
    assert handler._context is None

    # Each request got a distinct request_id
    assert len(handler.captured_request_ids) == 2
    assert handler.captured_request_ids[0] != handler.captured_request_ids[1]


@pytest.mark.integration
async def test_handler_error_custom_http_status():
    """G6.4: HandlerError with http_status=403 returns HTTP 403 (not 500)."""
    from application_sdk.handler.service import create_app_handler_service

    error_handler = _ErrorHandler()
    app = create_app_handler_service(error_handler, app_name="error-app")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/workflows/v1/auth", json={"credentials": []})

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Config & File Upload (tests 5–7)
# ---------------------------------------------------------------------------


@pytest.fixture
def state_app():
    """Create a handler service backed by a MockStateStore."""
    from application_sdk.handler.service import create_app_handler_service
    from application_sdk.testing.mocks import MockStateStore

    state = MockStateStore()
    handler = DefaultHandler()
    app = create_app_handler_service(handler, app_name="state-app", state_store=state)
    return app, state


@pytest.fixture
async def state_client(state_app):
    app, state = state_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, state


@pytest.mark.integration
async def test_config_round_trip(state_client):
    """G6.5: POST /config/{id} saves to state store; GET /config/{id} returns it."""
    client, state = state_client

    payload = {"host": "db.example.com", "port": 5432, "database": "mydb"}

    post_resp = await client.post("/workflows/v1/config/test-cfg", json=payload)
    assert post_resp.status_code == 200

    get_resp = await client.get("/workflows/v1/config/test-cfg")
    assert get_resp.status_code == 200
    data = get_resp.json()["data"]
    assert data["host"] == "db.example.com"
    assert data["port"] == 5432

    save_calls = state.get_save_calls()
    assert len(save_calls) == 1
    assert save_calls[0][0] == "workflows/test-cfg"


@pytest.mark.integration
async def test_config_not_found(state_client):
    """G6.6: GET /config/{id} returns 404 when key is not in state store."""
    client, _ = state_client

    resp = await client.get("/workflows/v1/config/nonexistent")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_file_upload_end_to_end(tmp_path):
    """G6.7: POST /file uploads a file to local obstore and returns FileUploadResponse shape."""
    from application_sdk.handler.service import create_app_handler_service
    from application_sdk.storage.factory import create_local_store
    from application_sdk.storage.ops import exists

    store = create_local_store(tmp_path / "uploads")
    handler = DefaultHandler()
    app = create_app_handler_service(handler, app_name="upload-app", storage=store)

    file_content = b"hello, integration test!"
    file_name = "test_data.txt"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/workflows/v1/file",
            files={"file": (file_name, file_content, "text/plain")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True

    data = body["data"]
    assert data["isUploaded"] is True
    assert data["fileSize"] == len(file_content)
    assert data["rawName"] == file_name
    assert data["extension"] == "txt"
    key = data["key"]
    assert key  # non-empty

    # Verify the file actually exists in the local store
    assert await exists(key, store=store, normalize=False)


# ---------------------------------------------------------------------------
# Workflow Lifecycle via HTTP (tests 8–11)
# ---------------------------------------------------------------------------
# These App/Input/Output classes must live at module level so Temporal's
# sandboxed workflow runner can import them by module path.  They are
# registered in AppRegistry/TaskRegistry at import time; clean_registries
# (autouse) resets both registries before each test, so fixtures call
# _reregister_app() to restore the entries before spinning up a worker.


@dataclass
class TrivialInput(Input):
    name: str = "world"


@dataclass
class TrivialOutput(Output):
    greeting: str = ""


class TrivialApp(App):
    async def run(self, input: TrivialInput) -> TrivialOutput:
        return TrivialOutput(greeting=f"Hello, {input.name}!")


@dataclass
class LongInput(Input):
    pass


@dataclass
class LongOutput(Output):
    done: bool = False


class LongRunningApp(App):
    @task(heartbeat_timeout_seconds=None, auto_heartbeat_seconds=None)
    async def long_sleep(self, input: LongInput) -> LongOutput:
        await asyncio.sleep(60)
        return LongOutput(done=True)

    async def run(self, input: LongInput) -> LongOutput:
        return await self.long_sleep(input)


def _reregister_app(app_cls: type) -> None:
    """Re-add an App class to AppRegistry and TaskRegistry.

    clean_registries (autouse) resets both registries before each test.
    Call this in fixtures that need the app registered for the worker.
    """
    from application_sdk.app.base import _register_tasks
    from application_sdk.app.registry import AppRegistry

    AppRegistry.get_instance().register(
        name=app_cls._app_name,
        version=app_cls.version,
        app_cls=app_cls,
        input_type=app_cls._input_type,
        output_type=app_cls._output_type,
        allow_override=True,
    )
    _register_tasks(app_cls, app_cls._app_name)


@pytest.fixture
async def trivial_workflow_app(temporal_client, task_queue):
    """Handler service configured with TrivialApp for workflow lifecycle tests."""
    from application_sdk.handler import service as svc
    from application_sdk.handler.service import create_app_handler_service

    _reregister_app(TrivialApp)

    handler = DefaultHandler()
    app = create_app_handler_service(
        handler,
        app_name="trivial",
        app_class=TrivialApp,
        temporal_host="localhost:7233",
        task_queue=task_queue,
    )
    # Inject the already-connected client to avoid a second connection
    svc._temporal_client = temporal_client

    return app, TrivialApp


@pytest.fixture
async def trivial_wf_client(trivial_workflow_app):
    app, app_cls = trivial_workflow_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, app_cls


@pytest.fixture
async def long_running_workflow_app(temporal_client, task_queue):
    """Handler service configured with LongRunningApp for the stop test."""
    from application_sdk.handler import service as svc
    from application_sdk.handler.service import create_app_handler_service

    _reregister_app(LongRunningApp)

    handler = DefaultHandler()
    app = create_app_handler_service(
        handler,
        app_name="long-running",
        app_class=LongRunningApp,
        temporal_host="localhost:7233",
        task_queue=task_queue,
    )
    svc._temporal_client = temporal_client

    return app


@pytest.mark.integration
async def test_start_workflow_via_http(run_worker, trivial_wf_client):
    """G6.8: POST /start returns workflow_id and run_id."""
    client, _ = trivial_wf_client

    async with run_worker():
        resp = await client.post("/workflows/v1/start", json={"name": "integration"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert "workflow_id" in data
    assert "run_id" in data
    assert data["workflow_id"]
    assert data["run_id"]


@pytest.mark.integration
async def test_start_and_get_result(run_worker, trivial_wf_client):
    """G6.9: Start workflow then GET /result with wait=true returns completed status."""
    client, _ = trivial_wf_client

    async with run_worker():
        start_resp = await client.post(
            "/workflows/v1/start", json={"name": "result-test"}
        )
        assert start_resp.status_code == 200
        wf_id = start_resp.json()["data"]["workflow_id"]

        result_resp = await client.get(
            f"/workflows/v1/result/{wf_id}", params={"wait": "true"}
        )

    assert result_resp.status_code == 200
    result_body = result_resp.json()
    assert result_body["success"] is True
    assert result_body["data"]["status"] == "completed"
    assert result_body["data"]["result"]["greeting"] == "Hello, result-test!"


@pytest.mark.integration
async def test_start_and_stop(run_worker, long_running_workflow_app):
    """G6.10: Start a long-running workflow, stop it, verify TERMINATED status."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=long_running_workflow_app),
        base_url="http://test",
    ) as client:
        async with run_worker():
            start_resp = await client.post("/workflows/v1/start", json={})
            assert start_resp.status_code == 200
            wf_id = start_resp.json()["data"]["workflow_id"]
            run_id = start_resp.json()["data"]["run_id"]

            # Poll until RUNNING to avoid race with terminate
            for _ in range(20):
                status_resp = await client.get(f"/workflows/v1/status/{wf_id}/{run_id}")
                if status_resp.json().get("data", {}).get("status") == "RUNNING":
                    break
                await asyncio.sleep(0.25)

            stop_resp = await client.post(f"/workflows/v1/stop/{wf_id}/{run_id}")
            assert stop_resp.status_code == 200
            assert stop_resp.json()["success"] is True

        # Check status after worker shuts down
        final_resp = await client.get(f"/workflows/v1/status/{wf_id}/{run_id}")
        assert final_resp.status_code == 200
        assert final_resp.json()["data"]["status"] == "TERMINATED"


@pytest.mark.integration
async def test_start_saves_config_to_state_store(run_worker, trivial_wf_client):
    """G6.11: Starting a workflow persists config (minus credentials) to state store."""
    from application_sdk.handler import service as svc
    from application_sdk.testing.mocks import MockStateStore

    client, _ = trivial_wf_client

    state = MockStateStore()
    svc._state_store = state

    async with run_worker():
        resp = await client.post(
            "/workflows/v1/start",
            json={"name": "state-test"},
        )
        assert resp.status_code == 200
        wf_id = resp.json()["data"]["workflow_id"]

    save_calls = state.get_save_calls()
    assert len(save_calls) == 1
    key, saved = save_calls[0]
    assert key == f"workflows/{wf_id}"
    assert saved["workflow_id"] == wf_id
    assert saved["name"] == "state-test"
    # Safety guard: credentials must never be persisted to the state store,
    # even if a migrating connector accidentally passes them in the /start body
    assert "credentials" not in saved
