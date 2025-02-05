from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.outputs.secretstore import SecretStoreOutput
from application_sdk.outputs.statestore import StateStoreOutput


class TestConfigurationAPI:
    def test_post_configuration_success(self, client: TestClient):
        """Test successful configuration creation/update"""
        # Mock the secretstore and statestore methods
        with patch.object(
            SecretStoreOutput, "store_credentials"
        ) as mock_store_creds, patch.object(
            StateStoreInput, "extract_configuration"
        ) as mock_extract_config, patch.object(
            StateStoreOutput, "store_configuration"
        ) as mock_store_config:
            mock_extract_config.return_value = {
                "credential_guid": "credential_test-abcd",
                "connection": {"connection": "production"},
                "metadata": {
                    "exclude-filter": "{}",
                    "include-filter": "{}",
                    "temp-table-regex": "^temp_",
                },
            }
            mock_store_creds.return_value = "credential_test-uuid"
            mock_store_config.return_value = "config_1234"

            payload = {
                "credential_guid": "credential_test-uuid",
                "connection": {"connection": "dev"},
                "metadata": {
                    "exclude-filter": "{}",
                    "include-filter": "{}",
                    "temp-table-regex": "",
                },
            }
            expected_config = payload.copy()

            response = client.post("/workflows/v1/config/1234", json=payload)

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["success"] is True
            assert (
                response_data["message"]
                == "Workflow configuration updated successfully"
            )
            assert response_data["data"] == expected_config

            # Verify StateStoreInput/StateStoreOutput interactions
            mock_extract_config.assert_called_once_with("1234")
            mock_store_config.assert_called_once()

    def test_post_configuration_partial_update(self, client: TestClient):
        """Test partial configuration update"""
        existing_config = {
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude-filter": "{}",
                "include-filter": "{}",
                "temp-table-regex": "",
            },
            "credential_guid": "old-credential-uuid",
        }

        # Only updating the connection part
        update_payload = {
            "connection": {"connection": "prod"},
        }

        expected_config = existing_config.copy()
        expected_config["connection"] = update_payload["connection"]

        with patch.object(
            StateStoreInput, "extract_configuration"
        ) as mock_extract_config, patch.object(
            StateStoreOutput, "store_configuration"
        ) as mock_store_config:
            mock_extract_config.return_value = existing_config.copy()
            mock_store_config.return_value = "1234"

            response = client.post("/workflows/v1/config/1234", json=update_payload)

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["success"] is True
            assert (
                response_data["message"]
                == "Workflow configuration updated successfully"
            )
            assert response_data["data"] == expected_config

            # Verify StateStoreInput/StateStoreOutput interactions
            mock_extract_config.assert_called_once()
            mock_store_config.assert_called_once_with("1234", expected_config)

    def test_get_configuration_success(self, client: TestClient):
        """Test successful configuration retrieval"""
        test_config = {
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude-filter": "{}",
                "include-filter": "{}",
                "temp-table-regex": "",
            },
            "credential_guid": "credential_test-uuid",
        }

        # Mock the StateStoreInput extract_configuration method
        with patch.object(
            StateStoreInput, "extract_configuration"
        ) as mock_extract_config:
            mock_extract_config.return_value = test_config

            response = client.get("/workflows/v1/config/1234")

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["success"] is True
            assert (
                response_data["message"]
                == "Workflow configuration fetched successfully"
            )
            assert response_data["data"] == test_config

            # Verify StateStoreInput interaction
            mock_extract_config.assert_called_once_with("1234")

    def test_get_configuration_not_found(self, client: TestClient):
        """Test configuration retrieval when not found"""
        with patch.object(
            StateStoreInput, "extract_configuration"
        ) as mock_extract_config:
            mock_extract_config.side_effect = ValueError(
                "State not found for key: config>>"
            )

            config_id = "nonexistent"

            # Test with a non-existent config ID
            with pytest.raises(ValueError):
                client.get(f"/workflows/v1/config/{config_id}")

    def test_post_configuration_store_error(self, client: TestClient):
        """Test configuration update when StateStoreInput throws an error"""
        payload = {
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude-filter": "{}",
                "include-filter": "{}",
                "temp-table-regex": "",
            },
        }

        with patch.object(
            StateStoreInput, "extract_configuration"
        ) as mock_extract_config:
            mock_extract_config.side_effect = ValueError(
                "Failed to extract configuration"
            )

            with pytest.raises(ValueError) as exc_info:
                client.post("/workflows/v1/config/1234", json=payload)

            assert exc_info.value.args[0] == "Failed to extract configuration"
