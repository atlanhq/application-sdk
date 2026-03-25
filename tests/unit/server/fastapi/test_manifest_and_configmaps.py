"""Tests for APIServer manifest route and list_configmaps endpoint."""

import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, patch

from httpx import ASGITransport, AsyncClient

from application_sdk.handlers import HandlerInterface
from application_sdk.server.fastapi import APIServer
from application_sdk.workflows import WorkflowInterface


class _MockHandler(HandlerInterface):
    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def load(self, config: Dict[str, Any]) -> None:
        pass

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        return True


class _MockWorkflow(WorkflowInterface):
    @staticmethod
    def get_activities(activities):
        return []


def _make_server(manifest=None, has_configmap=False):
    """Create an APIServer with optional manifest."""
    server = APIServer(
        handler=_MockHandler(),
        workflow_client=Mock(),
        ui_enabled=False,
        has_configmap=has_configmap,
        manifest=manifest,
    )
    return server


class TestManifestRoute:
    """Tests for the /manifest endpoint registration and response."""

    async def test_manifest_route_registered_when_manifest_provided(self):
        """When manifest is not None, /manifest route should be registered."""
        manifest_data = {"execution_mode": "automation-engine", "dag": {}}
        server = _make_server(manifest=manifest_data)

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/manifest")

        assert response.status_code == 200
        data = response.json()
        assert data["execution_mode"] == "automation-engine"

    async def test_manifest_route_not_registered_when_none(self):
        """When manifest is None, /manifest route should not exist."""
        server = _make_server(manifest=None)

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/manifest")

        # Should get 404 or 405 since route doesn't exist
        assert response.status_code in (404, 405)

    async def test_manifest_returns_exact_data(self):
        """Manifest endpoint returns the exact dict passed at init time."""
        manifest_data = {
            "execution_mode": "automation-engine",
            "dag": {
                "extract": {"activity_name": "execute_workflow"},
                "publish": {"depends_on": {"node_id": "extract"}},
            },
        }
        server = _make_server(manifest=manifest_data)

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/manifest")

        assert response.status_code == 200
        assert response.json() == manifest_data


class TestListConfigmaps:
    """Tests for the /workflows/v1/configmaps endpoint."""

    async def test_list_configmaps_returns_empty_when_no_dir(self):
        """When app/generated doesn't exist, returns empty list."""
        server = _make_server()

        with patch(
            "application_sdk.handlers.CONTRACT_GENERATED_DIR",
            Path("/nonexistent/path"),
        ):
            transport = ASGITransport(app=server.app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/workflows/v1/configmaps")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"] == []

    async def test_list_configmaps_excludes_manifest(self):
        """manifest.json is excluded from the configmap listing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "contract" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text("{}")
            (gen_dir / "my-config.json").write_text("{}")
            (gen_dir / "other-config.json").write_text("{}")

            server = _make_server()
            with patch("application_sdk.handlers.CONTRACT_GENERATED_DIR", gen_dir):
                transport = ASGITransport(app=server.app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.get("/workflows/v1/configmaps")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        ids = data["data"]
        assert "manifest" not in ids
        assert "my-config" in ids
        assert "other-config" in ids

    async def test_list_configmaps_empty_dir(self):
        """Empty app/generated returns empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "contract" / "generated"
            gen_dir.mkdir(parents=True)

            server = _make_server()
            with patch("application_sdk.handlers.CONTRACT_GENERATED_DIR", gen_dir):
                transport = ASGITransport(app=server.app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.get("/workflows/v1/configmaps")

        assert response.status_code == 200
        assert response.json()["data"] == []
