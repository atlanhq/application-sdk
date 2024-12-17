from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.inputs.statestore import StateStore
from application_sdk.workflows.sql.controllers.metadata import (
    SQLWorkflowMetadataController,
)


class TestConfigurationAPI:
    @pytest.fixture
    def mock_sql_resource(self) -> Any:
        mock = Mock()
        mock.fetch_metadata = AsyncMock()
        mock.sql_input = AsyncMock()
        return mock

    @pytest.fixture
    def controller(self, mock_sql_resource: Any) -> SQLWorkflowMetadataController:
        controller = SQLWorkflowMetadataController(mock_sql_resource)
        return controller

    @pytest.fixture
    def app(self, controller: SQLWorkflowMetadataController) -> FastAPIApplication:
        """Create FastAPI test application"""
        app = FastAPIApplication(metadata_controller=controller)
        app.register_routers()
        app.register_routes()
        return app

    @pytest.fixture
    def client(self, app: FastAPIApplication) -> TestClient:
        """Create test client"""
        return TestClient(app.app)

    def test_post_configuration_success(self, client: TestClient):
        """Test successful configuration creation/update"""
        # Mock the StateStore methods
        with patch.object(
            StateStore, "store_credentials"
        ) as mock_store_creds, patch.object(
            StateStore, "store_configuration"
        ) as mock_store_config:
            mock_store_creds.return_value = "credential_test-uuid"
            mock_store_config.return_value = "config_1234"

            payload = {
                "credentials": {
                    "host": "localhost",
                    "port": "5432",
                    "user": "postgres",
                    "password": "password",
                    "database": "postgres",
                },
                "connection": {"connection": "dev"},
                "metadata": {
                    "exclude_filter": "{}",
                    "include_filter": "{}",
                    "temp_table_regex": "",
                    "advanced_config_strategy": "default",
                },
            }
            expected_config = {
                "connection": payload["connection"],
                "metadata": payload["metadata"],
                "credential_guid": "credential_test-uuid",
                "workflow_config_guid": "config_1234",
            }

            response = client.post("/workflows/v1/config/1234", json=payload)

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["success"] is True
            assert (
                response_data["message"]
                == "Workflow configuration updated successfully"
            )
            assert response_data["data"] == expected_config

            # Verify StateStore interactions
            mock_store_creds.assert_called_once_with(payload["credentials"])
            mock_store_config.assert_called_once()

    def test_get_configuration_success(self, client: TestClient):
        """Test successful configuration retrieval"""
        test_config = {
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude_filter": "{}",
                "include_filter": "{}",
                "temp_table_regex": "",
            },
            "credential_guid": "credential_test-uuid",
        }

        # Mock the StateStore extract_configuration method
        with patch.object(StateStore, "extract_configuration") as mock_extract_config:
            mock_extract_config.return_value = test_config

            response = client.get("/workflows/v1/config/config_1234")

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["success"] is True
            assert (
                response_data["message"]
                == "Workflow configuration fetched successfully"
            )
            assert response_data["data"] == test_config

            # Verify StateStore interaction
            mock_extract_config.assert_called_once_with("config_1234")

    def test_get_configuration_not_found(self, client: TestClient):
        """Test configuration retrieval when not found"""
        with patch.object(StateStore, "extract_configuration") as mock_extract_config:
            mock_extract_config.side_effect = ValueError(
                "State not found for key: config>>"
            )

            config_id = "config_nonexistent"

            # Test with a non-existent config ID
            with pytest.raises(ValueError):
                client.get(f"/workflows/v1/config/{config_id}")

                # assert response.status_code == 500
                # response_data = response.json()
                # assert response_data == {
                #     "error": "An internal error has occurred.",
                #     "details": "State not found for key: config>>"
                # }

    def test_post_configuration_invalid_payload(self, client: TestClient):
        """Test configuration creation with invalid payload"""
        invalid_payload: Dict[str, Any] = {
            # "credentials": {},  # Missing required fields
            "connection": {},
            "metadata": {},
        }

        response = client.post("/workflows/v1/config/1234", json=invalid_payload)
        assert response.status_code == 422  # Validation error
