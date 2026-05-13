"""Unit tests for ``application_sdk.clients.azure.client``.

``AzureClient.load`` contains a function-local import at line ~140::

    from application_sdk.infrastructure import (
        AsyncDaprClient, DaprCredentialVault,
    )

These tests:

* Exercise the inline import on the ``credential_guid`` branch and assert the
  symbols *exist* on the package.
* Cover ``load`` error mapping for every documented exception type.
* Cover ``close`` happy-path and exception-swallowing for service clients
  with ``close``, ``disconnect``, neither, and one that raises.
* Cover ``_test_connection`` happy-path and every error class.
* Cover both context managers, including the ``__exit__`` branch where no
  event loop is running.

All Azure SDK / Dapr / heartbeat collaborators are mocked. No real I/O.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import AzureError, ClientAuthenticationError

from application_sdk.clients.azure._azure_errors import (
    AzureAuthError,
    AzureCredentialParseError,
    AzureNoCredentialError,
)
from application_sdk.clients.azure.client import (
    AzureClient,
    HealthStatus,
    ServiceHealth,
)
from application_sdk.errors import AppError

# ---------------------------------------------------------------------------
# Inline-import contract
# ---------------------------------------------------------------------------


class TestInfrastructureSymbolContract:
    """The inline import in ``load()`` should resolve via the public package."""

    def test_async_dapr_client_importable_from_infrastructure(self):
        from application_sdk.infrastructure import AsyncDaprClient  # noqa: F401

    def test_dapr_credential_vault_importable_from_infrastructure(self):
        from application_sdk.infrastructure import DaprCredentialVault  # noqa: F401

    async def test_inline_import_executes_on_credential_guid_path(self):
        """Drive the inline import branch and assert it reaches the vault.

        We patch ``AsyncDaprClient`` and ``DaprCredentialVault`` *as attributes
        of the package* — that's how ``from application_sdk.infrastructure
        import X`` resolves them at call time. If either name is renamed in
        the source, the inline import line raises ImportError and this test
        fails loudly instead of surfacing only at runtime.
        """
        client = AzureClient(credentials={"credential_guid": "g-456"})
        client.auth_provider = MagicMock()
        client.auth_provider.create_credential = AsyncMock(return_value=MagicMock())

        fake_dapr = AsyncMock()
        fake_vault = MagicMock()
        fake_vault.get_credentials = AsyncMock(
            return_value={"tenant_id": "t", "client_id": "c", "client_secret": "s"}
        )
        with (
            patch(
                "application_sdk.infrastructure.AsyncDaprClient",
                return_value=fake_dapr,
            ) as dapr_ctor,
            patch(
                "application_sdk.infrastructure.DaprCredentialVault",
                return_value=fake_vault,
            ) as vault_ctor,
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await client.load()
        dapr_ctor.assert_called_once()
        vault_ctor.assert_called_once_with(fake_dapr)
        fake_vault.get_credentials.assert_awaited_once_with("g-456")
        fake_dapr.close.assert_awaited_once()
        assert client._connection_health is True

    async def test_dapr_client_closed_even_on_vault_failure(self):
        """``finally: await dapr_client.close()`` must run even when the
        vault raises — connection-leak guard."""
        client = AzureClient(credentials={"credential_guid": "g-err"})

        fake_dapr = AsyncMock()
        fake_vault = MagicMock()
        fake_vault.get_credentials = AsyncMock(side_effect=RuntimeError("kaboom"))
        with (
            patch(
                "application_sdk.infrastructure.AsyncDaprClient",
                return_value=fake_dapr,
            ),
            patch(
                "application_sdk.infrastructure.DaprCredentialVault",
                return_value=fake_vault,
            ),
            pytest.raises(AppError),
        ):
            await client.load()
        fake_dapr.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_init(self):
        c = AzureClient()
        assert c.credentials == {}
        assert c.resolved_credentials == {}
        assert c.credential is None
        assert c.auth_provider is not None
        assert c._services == {}
        assert c._connection_health is False
        # default executor with 10 workers
        assert c._executor is not None

    def test_kwargs_stored(self):
        c = AzureClient(credentials={"foo": "bar"}, max_workers=4, extra="x")
        assert c.credentials == {"foo": "bar"}
        assert c._kwargs == {"extra": "x"}

    def test_init_credentials_none_becomes_empty(self):
        c = AzureClient(credentials=None)
        assert c.credentials == {}


# ---------------------------------------------------------------------------
# load() — direct credentials & warning paths
# ---------------------------------------------------------------------------


def _direct_credentials() -> dict[str, Any]:
    return {"tenant_id": "t", "client_id": "c", "client_secret": "s"}


class TestLoadDirectAndWarning:
    async def test_load_with_direct_credentials_uses_them_as_is(self):
        client = AzureClient()
        client.auth_provider = MagicMock()
        client.auth_provider.create_credential = AsyncMock(return_value=MagicMock())

        with patch(
            "application_sdk.clients.azure.client.run_in_thread",
            new=AsyncMock(return_value=MagicMock()),
        ):
            await client.load(credentials=_direct_credentials())

        assert client.resolved_credentials == _direct_credentials()
        client.auth_provider.create_credential.assert_awaited_once_with(
            auth_type="service_principal",
            credentials=_direct_credentials(),
        )
        assert client._connection_health is True

    async def test_load_warns_when_secret_path_present_without_guid(self):
        client = AzureClient(credentials={"secret-path": "vault/x"})
        client.auth_provider = MagicMock()
        client.auth_provider.create_credential = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("application_sdk.clients.azure.client.logger") as mock_logger,
        ):
            await client.load()
        # the WARN call must have fired
        assert mock_logger.warning.called

    async def test_load_warns_when_credential_source_present_without_guid(self):
        client = AzureClient(credentials={"credentialSource": "x"})
        client.auth_provider = MagicMock()
        client.auth_provider.create_credential = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("application_sdk.clients.azure.client.logger") as mock_logger,
        ):
            await client.load()
        assert mock_logger.warning.called

    async def test_load_credentials_param_overrides_init_credentials(self):
        client = AzureClient(credentials={"tenant_id": "old"})
        client.auth_provider = MagicMock()
        client.auth_provider.create_credential = AsyncMock(return_value=MagicMock())

        new = _direct_credentials()
        with patch(
            "application_sdk.clients.azure.client.run_in_thread",
            new=AsyncMock(return_value=MagicMock()),
        ):
            await client.load(credentials=new)
        assert client.credentials == new


# ---------------------------------------------------------------------------
# load() — error mapping
# ---------------------------------------------------------------------------


class TestLoadErrorMapping:
    @pytest.fixture
    def loaded_client(self):
        c = AzureClient(credentials=_direct_credentials())
        c.auth_provider = MagicMock()
        return c

    async def test_client_authentication_error_maps_to_auth_error(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=ClientAuthenticationError("bad creds")
        )
        with pytest.raises(AzureAuthError) as ei:
            await loaded_client.load()
        assert "bad creds" in str(ei.value)

    async def test_azure_error_maps_to_auth_error(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=AzureError("svc down")
        )
        with pytest.raises(AzureAuthError) as ei:
            await loaded_client.load()
        assert "svc down" in str(ei.value)

    async def test_value_error_maps_to_credential_parse_error(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=ValueError("bad input")
        )
        with pytest.raises(AzureCredentialParseError) as ei:
            await loaded_client.load()
        assert "Invalid parameters" in str(ei.value)

    async def test_type_error_maps_to_credential_parse_error(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=TypeError("bad type")
        )
        with pytest.raises(AzureCredentialParseError) as ei:
            await loaded_client.load()
        assert "Invalid parameter types" in str(ei.value)

    async def test_generic_exception_maps_to_auth_error(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=RuntimeError("?!?!")
        )
        with pytest.raises(AzureAuthError) as ei:
            await loaded_client.load()
        assert "Unexpected error" in str(ei.value)

    async def test_load_failure_does_not_set_connection_health(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            side_effect=RuntimeError("nope")
        )
        with pytest.raises(AppError):
            await loaded_client.load()
        assert loaded_client._connection_health is False

    async def test_test_connection_failure_propagates_via_load(self, loaded_client):
        loaded_client.auth_provider.create_credential = AsyncMock(
            return_value=MagicMock()
        )
        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(side_effect=ClientAuthenticationError("test failed")),
            ),
            pytest.raises(AzureAuthError) as ei,
        ):
            await loaded_client.load()
        assert "test failed" in str(ei.value)


# ---------------------------------------------------------------------------
# _test_connection direct
# ---------------------------------------------------------------------------


class TestTestConnection:
    async def test_no_credential_raises_no_credential_error(self):
        client = AzureClient()
        client.credential = None
        with pytest.raises(AzureNoCredentialError) as ei:
            await client._test_connection()
        assert "No credential available" in str(ei.value)

    async def test_success_path_calls_run_in_thread_with_endpoint(self):
        from application_sdk.clients.azure import AZURE_MANAGEMENT_API_ENDPOINT

        client = AzureClient()
        client.credential = MagicMock()
        get_token = client.credential.get_token
        run_in_thread_mock = AsyncMock(return_value=MagicMock())
        with patch(
            "application_sdk.clients.azure.client.run_in_thread",
            new=run_in_thread_mock,
        ):
            await client._test_connection()
        run_in_thread_mock.assert_awaited_once_with(
            get_token, AZURE_MANAGEMENT_API_ENDPOINT
        )

    async def test_client_authentication_error_mapped(self):
        client = AzureClient()
        client.credential = MagicMock()
        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(side_effect=ClientAuthenticationError("bad")),
            ),
            pytest.raises(AzureAuthError) as ei,
        ):
            await client._test_connection()
        assert "bad" in str(ei.value)

    async def test_azure_error_mapped(self):
        client = AzureClient()
        client.credential = MagicMock()
        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(side_effect=AzureError("down")),
            ),
            pytest.raises(AzureAuthError) as ei,
        ):
            await client._test_connection()
        assert "down" in str(ei.value)

    async def test_value_error_mapped(self):
        client = AzureClient()
        client.credential = MagicMock()
        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(side_effect=ValueError("nope")),
            ),
            pytest.raises(AzureCredentialParseError) as ei,
        ):
            await client._test_connection()
        assert "Invalid parameters" in str(ei.value)

    async def test_unexpected_error_mapped(self):
        client = AzureClient()
        client.credential = MagicMock()
        with (
            patch(
                "application_sdk.clients.azure.client.run_in_thread",
                new=AsyncMock(side_effect=RuntimeError("?!?!")),
            ),
            pytest.raises(AzureAuthError) as ei,
        ):
            await client._test_connection()
        assert "Unexpected error" in str(ei.value)


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_with_no_services_resets_state(self):
        client = AzureClient()
        client._connection_health = True
        await client.close()
        assert client._services == {}
        assert client._connection_health is False

    async def test_close_calls_close_on_each_service(self):
        client = AzureClient()
        s1 = MagicMock()
        s1.close = AsyncMock()
        s2 = MagicMock()
        s2.close = AsyncMock()
        client._services = {"a": s1, "b": s2}
        await client.close()
        s1.close.assert_awaited_once()
        s2.close.assert_awaited_once()
        assert client._services == {}

    async def test_close_falls_back_to_disconnect_when_no_close(self):
        client = AzureClient()
        s = MagicMock(spec=["disconnect"])
        s.disconnect = AsyncMock()
        client._services = {"a": s}
        await client.close()
        s.disconnect.assert_awaited_once()

    async def test_close_skips_service_with_neither_method(self):
        client = AzureClient()
        s = MagicMock(spec=[])  # no close, no disconnect
        client._services = {"a": s}
        # must not raise
        await client.close()
        assert client._services == {}

    async def test_close_swallows_service_exception(self):
        client = AzureClient()
        bad = MagicMock()
        bad.close = AsyncMock(side_effect=RuntimeError("won't close"))
        good = MagicMock()
        good.close = AsyncMock()
        client._services = {"bad": bad, "good": good}
        # must not raise; good service still closed
        await client.close()
        good.close.assert_awaited_once()
        assert client._services == {}

    async def test_close_idempotent(self):
        client = AzureClient()
        await client.close()
        await client.close()  # second call must not raise
        assert client._connection_health is False


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


class TestContextManagers:
    def test_sync_enter_returns_self(self):
        client = AzureClient()
        assert client.__enter__() is client

    def test_sync_exit_with_no_running_loop_does_not_raise(self):
        """``__exit__`` outside an event loop must log + continue."""
        client = AzureClient()
        # there's no running loop here — covers the RuntimeError branch
        client.__exit__(None, None, None)

    def test_sync_exit_with_running_loop_schedules_close(self):
        """``__exit__`` schedules ``close`` as a task when a loop is running."""

        async def _run():
            client = AzureClient()
            with patch.object(client, "close", new=AsyncMock()) as mock_close:
                client.__exit__(None, None, None)
                # Yield once so the scheduled task runs
                await asyncio.sleep(0)
                mock_close.assert_awaited_once()

        asyncio.run(_run())

    async def test_async_enter_returns_self(self):
        client = AzureClient()
        ret = await client.__aenter__()
        assert ret is client

    async def test_async_aexit_calls_close(self):
        client = AzureClient()
        with patch.object(client, "close", new=AsyncMock()) as mock_close:
            await client.__aexit__(None, None, None)
            mock_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# health_check — additional edge cases
# ---------------------------------------------------------------------------


class TestHealthCheckEdgeCases:
    async def test_overall_health_false_when_connection_unhealthy_even_with_services(
        self,
    ):
        client = AzureClient()
        client._connection_health = False
        client._services = {"x": MagicMock()}
        # Even though services exist, connection is unhealthy → return early
        result = await client.health_check()
        assert result.connection_health is False
        assert result.services == {}
        assert result.overall_health is False

    async def test_returns_health_status_instance(self):
        client = AzureClient()
        result = await client.health_check()
        assert isinstance(result, HealthStatus)


# ---------------------------------------------------------------------------
# Pydantic models edge cases
# ---------------------------------------------------------------------------


class TestModels:
    def test_service_health_serialization(self):
        h = ServiceHealth(status="healthy")
        assert h.model_dump() == {"status": "healthy", "error": None}

    def test_health_status_serialization(self):
        s = HealthStatus(connection_health=True, services={}, overall_health=False)
        d = s.model_dump()
        assert d["connection_health"] is True
        assert d["services"] == {}
        assert d["overall_health"] is False


# Regression coverage for these client-error paths lives in
# tests/unit/clients/test_clienterror_preservation.py.
