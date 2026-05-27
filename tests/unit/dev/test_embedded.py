"""Unit tests for ``application_sdk.dev._embedded``.

Covers the lifecycle invariants of ``embedded_runtime`` without actually
downloading or spawning Temporal's dev-server binary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.dev._embedded import EmbeddedRuntime, embedded_runtime


@pytest.mark.asyncio
class TestEmbeddedRuntimeLifecycle:
    async def test_yields_embedded_runtime_dataclass_and_shuts_down(self) -> None:
        """Yielded value carries the loopback target; shutdown is awaited on exit."""
        fake_env = MagicMock()
        fake_env.client.service_client.config.target_host = "127.0.0.1:54321"
        fake_env.shutdown = AsyncMock(return_value=None)

        start_local = AsyncMock(return_value=fake_env)
        with patch(
            "temporalio.testing.WorkflowEnvironment.start_local",
            start_local,
        ):
            async with embedded_runtime(namespace="ns1") as rt:
                assert isinstance(rt, EmbeddedRuntime)
                assert rt.host == "127.0.0.1:54321"
                assert rt.namespace == "ns1"
                assert rt.ui_url is None

        fake_env.shutdown.assert_awaited_once()
        assert start_local.await_args.kwargs["ui"] is False
        assert start_local.await_args.kwargs["ui_port"] is None

    async def test_can_enable_temporal_ui(self) -> None:
        """Temporal Web UI can be enabled with the default SDK port."""
        fake_env = MagicMock()
        fake_env.client.service_client.config.target_host = "127.0.0.1:54321"
        fake_env.shutdown = AsyncMock(return_value=None)

        start_local = AsyncMock(return_value=fake_env)
        with patch(
            "temporalio.testing.WorkflowEnvironment.start_local",
            start_local,
        ):
            async with embedded_runtime(temporal_ui=True) as rt:
                assert rt.ui_url == "http://127.0.0.1:8233"

        fake_env.shutdown.assert_awaited_once()
        assert start_local.await_args.kwargs["ui"] is True
        assert start_local.await_args.kwargs["ui_port"] == 8233

    async def test_can_override_temporal_ui_port(self) -> None:
        """Temporal Web UI can bind to an explicit port when requested."""
        fake_env = MagicMock()
        fake_env.client.service_client.config.target_host = "127.0.0.1:54321"
        fake_env.shutdown = AsyncMock(return_value=None)

        start_local = AsyncMock(return_value=fake_env)
        with patch(
            "temporalio.testing.WorkflowEnvironment.start_local",
            start_local,
        ):
            async with embedded_runtime(temporal_ui=True, temporal_ui_port=9233) as rt:
                assert rt.ui_url == "http://127.0.0.1:9233"

        fake_env.shutdown.assert_awaited_once()
        assert start_local.await_args.kwargs["ui"] is True
        assert start_local.await_args.kwargs["ui_port"] == 9233

    async def test_shutdown_runs_even_when_body_raises(self) -> None:
        """The runtime is torn down even if the caller raises inside the context."""
        fake_env = MagicMock()
        fake_env.client.service_client.config.target_host = "127.0.0.1:54321"
        fake_env.shutdown = AsyncMock(return_value=None)

        with (
            patch(
                "temporalio.testing.WorkflowEnvironment.start_local",
                AsyncMock(return_value=fake_env),
            ),
            pytest.raises(ValueError),
        ):
            async with embedded_runtime() as _rt:
                raise ValueError("boom")

        fake_env.shutdown.assert_awaited_once()
