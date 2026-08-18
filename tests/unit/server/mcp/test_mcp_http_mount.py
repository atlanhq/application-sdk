"""Tests for the MCP server as it is actually served: mounted on the handler.

``create_app_handler_service(..., ENABLE_MCP)`` mounts the FastMCP app on the
FastAPI handler under ``/mcp`` from inside the lifespan. That mount — real
FastAPI, real starlette, real fastmcp streamable HTTP — is the path an
Automation Engine agent talks to, and it is the path that broke on a tenant
while the mocked unit tests stayed green. These tests speak MCP over the ASGI
transport (no port, no subprocess, no Temporal) so the wire shape is asserted,
not assumed.

Test Case Summary:
+------------------------------------------+-------------------------------------------+
| Test                                     | Purpose                                   |
+------------------------------------------+-------------------------------------------+
| test_initialize_handshake_succeeds       | lifespan mounts /mcp and the session      |
|                                          | handshake completes on the pinned fastmcp |
| test_tools_list_advertises_only_input    | tools/list over HTTP: the schema an agent |
|                                          | receives carries `input` only, no `self`  |
| test_tools_call_executes_task            | tools/call runs the task body in-process  |
|                                          | and returns its Output                    |
| test_missing_mcp_extra_raises_actionable | ENABLE_MCP without the `mcp` extra fails  |
|                                          | with install instructions, not            |
|                                          | ModuleNotFoundError                       |
+------------------------------------------+-------------------------------------------+
"""

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from application_sdk import constants
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.service import create_app_handler_service
from tests.unit.server.mcp.conftest import APP_NAME, ProbeApp

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass
class McpSession:
    """A minimal MCP streamable-HTTP client over the mounted ASGI route.

    fastmcp's own client cannot drive an ASGI app in-process, so the JSON-RPC
    envelope is spelled out here — which also makes the assertions be about the
    bytes an agent receives.
    """

    client: TestClient
    session_id: str = ""
    next_id: int = 1

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its JSON-RPC ``result``."""
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": method,
                "params": params or {},
            },
            headers=self._headers(),
        )
        self.next_id += 1
        assert response.status_code == 200, response.text
        self.session_id = self.session_id or response.headers.get("mcp-session-id", "")
        payload = _sse_json(response.text)
        assert "error" not in payload, payload
        return payload["result"]

    def notify(self, method: str) -> None:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": method},
            headers=self._headers(),
        )
        assert response.status_code == 202, response.text

    def _headers(self) -> dict[str, str]:
        headers = dict(_MCP_HEADERS)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers


def _sse_json(body: str) -> dict[str, Any]:
    """Extract the JSON-RPC payload from a text/event-stream response."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"no SSE data frame in response: {body!r}")


@pytest.fixture(autouse=True)
def _reset_service_globals() -> Iterator[None]:
    """Keep the handler service's module globals from leaking across tests."""
    from application_sdk.handler import service as svc
    from application_sdk.infrastructure import clear_infrastructure

    def _reset() -> None:
        svc._temporal_client = None
        svc._workflow_config = svc.WorkflowClientConfig()
        svc._secret_store = None
        svc._storage = None
        clear_infrastructure()

    _reset()
    yield
    _reset()


@pytest.fixture
def session(probe: ProbeApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[McpSession]:
    """An initialized MCP session against the mounted handler service."""
    monkeypatch.setattr(constants, "ENABLE_MCP", True)
    app = create_app_handler_service(DefaultHandler(), app_name=APP_NAME)

    # Entering the TestClient runs the lifespan, which is what mounts /mcp.
    with TestClient(app) as client:
        mcp = McpSession(client=client)
        mcp.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1.0"},
            },
        )
        mcp.notify("notifications/initialized")
        yield mcp


class TestMountedMcpRoute:
    def test_initialize_handshake_succeeds(self, session: McpSession) -> None:
        """The session fixture completed the handshake, so /mcp is really mounted."""
        assert session.session_id

    def test_tools_list_advertises_only_input(self, session: McpSession) -> None:
        result = session.rpc("tools/list")

        tools = {tool["name"]: tool for tool in result["tools"]}
        assert set(tools) == {"fetch_schemas"}
        schema = tools["fetch_schemas"]["inputSchema"]
        assert set(schema["properties"]) == {"input"}
        assert "self" not in json.dumps(schema)

    def test_tools_call_executes_task(
        self, session: McpSession, probe: ProbeApp
    ) -> None:
        result = session.rpc(
            "tools/call",
            {"name": "fetch_schemas", "arguments": {"input": {"value": "http"}}},
        )

        assert result["isError"] is False
        assert result["structuredContent"]["result"] == "schemas:http"
        assert len(probe.instances) == 1
        assert isinstance(probe.instances[0], probe.app_cls)


class TestMissingExtra:
    def test_missing_mcp_extra_raises_actionable_error(
        self, probe: ProbeApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENABLE_MCP without the extra installed must say what to install.

        ``None`` in sys.modules makes ``import fastmcp`` raise a
        ``ModuleNotFoundError`` named ``fastmcp`` — the real signal that the
        ``mcp`` extra is not installed. The lifespan catches that specific case
        and rewrites it with install instructions; an unrelated broken import
        inside the chain is re-raised unchanged (see the sibling test).
        """
        monkeypatch.setattr(constants, "ENABLE_MCP", True)
        # Evict every cached application_sdk.server.mcp* and fastmcp* module so
        # the `from fastmcp import FastMCP` at the top of server.py re-runs and
        # hits the nulled fastmcp below — earlier tests in this module import
        # the real chain and leave it in sys.modules.
        for mod in [
            m
            for m in sys.modules
            if m == "fastmcp"
            or m.startswith("fastmcp.")
            or m == "application_sdk.server.mcp"
            or m.startswith("application_sdk.server.mcp.")
        ]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.setitem(sys.modules, "fastmcp", None)

        with pytest.raises(RuntimeError, match="'mcp' extra"):
            create_app_handler_service(DefaultHandler(), app_name=APP_NAME)

    def test_unrelated_import_error_is_reraised_unchanged(
        self, probe: ProbeApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken import *inside* the MCP chain must not be misreported.

        If ``application_sdk.server.mcp`` itself fails to import for a reason
        other than the extra being absent (an SDK-internal bug, a broken
        transitive dep), the broad "install the mcp extra" message would send
        the user to reinstall when the fault is elsewhere. The lifespan must
        re-raise that ``ModuleNotFoundError`` unchanged instead.
        """
        monkeypatch.setattr(constants, "ENABLE_MCP", True)
        monkeypatch.setitem(sys.modules, "application_sdk.server.mcp", None)

        with pytest.raises(ModuleNotFoundError):
            create_app_handler_service(DefaultHandler(), app_name=APP_NAME)
