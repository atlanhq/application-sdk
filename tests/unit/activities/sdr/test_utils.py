"""Tests for SDR create_handler utility function."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.utils import create_handler


class TestCreateHandler:
    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.utils.SecretStore")
    async def test_create_handler_with_credential_guid(
        self, mock_secret_store: MagicMock
    ):
        mock_client_class = MagicMock()
        mock_handler_instance = AsyncMock()
        # Non-SQL handler: has 'client' attribute, no 'sql_client'
        del mock_handler_instance.sql_client
        mock_handler_class = MagicMock(return_value=mock_handler_instance)

        mock_secret_store.get_credentials = AsyncMock(
            return_value={"username": "test", "password": "secret"}
        )

        workflow_args = {"credential_guid": "guid-123"}

        result = await create_handler(  # type: ignore[arg-type]
            mock_client_class,
            mock_handler_class,
            workflow_args,
        )

        mock_client_class.assert_called_once()
        mock_handler_class.assert_called_once()
        # Client is assigned to handler.client (non-SQL path)
        assert mock_handler_instance.client == mock_client_class.return_value
        mock_secret_store.get_credentials.assert_awaited_once_with(
            credential_guid="guid-123"
        )
        mock_handler_instance.load.assert_awaited_once_with(
            {"username": "test", "password": "secret"}
        )
        assert result is mock_handler_instance

    @pytest.mark.asyncio
    async def test_create_handler_with_credentials_dict(self):
        mock_client_class = MagicMock()
        mock_handler_instance = AsyncMock()
        del mock_handler_instance.sql_client
        mock_handler_class = MagicMock(return_value=mock_handler_instance)

        creds = {"username": "test", "password": "secret"}
        workflow_args = {"credentials": creds}

        result = await create_handler(  # type: ignore[arg-type]
            mock_client_class,
            mock_handler_class,
            workflow_args,
        )

        mock_handler_instance.load.assert_awaited_once_with(creds)
        assert result is mock_handler_instance

    @pytest.mark.asyncio
    async def test_create_handler_raises_without_credentials(self):
        mock_client_class = MagicMock()
        mock_handler_class = MagicMock()

        with pytest.raises(ValueError, match="credential_guid.*credentials"):
            await create_handler(  # type: ignore[arg-type]
                mock_client_class,
                mock_handler_class,
                {},
            )

    @pytest.mark.asyncio
    async def test_create_handler_assigns_sql_client_for_sql_handlers(self):
        """SQL handlers expose sql_client; create_handler should assign to it."""
        mock_client_class = MagicMock()
        mock_handler_instance = AsyncMock()
        # SQL handler: has 'sql_client' attribute
        mock_handler_instance.sql_client = None
        mock_handler_class = MagicMock(return_value=mock_handler_instance)

        creds = {"username": "test", "password": "secret"}
        workflow_args = {"credentials": creds}

        result = await create_handler(  # type: ignore[arg-type]
            mock_client_class,
            mock_handler_class,
            workflow_args,
        )

        # Client is assigned to handler.sql_client (SQL path)
        assert mock_handler_instance.sql_client == mock_client_class.return_value
        mock_handler_instance.load.assert_awaited_once_with(creds)
        assert result is mock_handler_instance
