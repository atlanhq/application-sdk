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

        with patch(
            "temporalio.testing.WorkflowEnvironment.start_local",
            AsyncMock(return_value=fake_env),
        ):
            async with embedded_runtime(namespace="ns1") as rt:
                assert isinstance(rt, EmbeddedRuntime)
                assert rt.host == "127.0.0.1:54321"
                assert rt.namespace == "ns1"

        fake_env.shutdown.assert_awaited_once()

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
