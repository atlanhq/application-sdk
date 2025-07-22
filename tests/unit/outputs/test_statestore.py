import json
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.constants import STATE_STORE_NAME
from application_sdk.outputs.secretstore import SecretStoreOutput
from application_sdk.outputs.statestore import StateStoreOutput
from application_sdk.test_utils.hypothesis.strategies.outputs.statestore import (
    configuration_strategy,
    credentials_strategy,
    uuid_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "statestore_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("statestore_tests")


@pytest.fixture
def mock_dapr_output_client() -> Generator[MagicMock, None, None]:
    with patch("application_sdk.outputs.statestore.DaprClient") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.__exit__.return_value = None
        yield mock_instance


def test_state_store_name() -> None:
    assert STATE_STORE_NAME == "statestore"


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Cannot create a collection of min_size=213 unique elements with values drawn from only 17 distinct elements"
)
@given(config=configuration_strategy(), uuid=uuid_strategy)  # type: ignore
def test_store_configuration_success(
    mock_dapr_output_client: MagicMock, config: Dict[str, Any], uuid: str
) -> None:
    mock_dapr_output_client.reset_mock()  # Reset mock between examples
    result = StateStoreOutput.store_configuration(uuid, config)

    assert result == uuid
    mock_dapr_output_client.save_state.assert_called_once_with(
        store_name="statestore", key=f"config_{uuid}", value=json.dumps(config)
    )


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Cannot create a collection of min_size=11383 unique elements with values drawn from only 17 distinct elements"
)
@given(config=credentials_strategy())  # type: ignore
async def test_store_credentials_success(
    mock_dapr_output_client: MagicMock, config: Dict[str, Any]
) -> None:
    mock_dapr_output_client.reset_mock()  # Reset mock between examples
    with patch("uuid.uuid4", return_value="test-uuid"):
        result = await SecretStoreOutput.save_secret(config)

    assert result == "test-uuid"
    mock_dapr_output_client.save_state.assert_called_once_with(
        store_name="statestore", key="credential_test-uuid", value=json.dumps(config)
    )


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Cannot create a collection of min_size=1019 unique elements with values drawn from only 17 distinct elements"
)
@given(config=credentials_strategy())  # type: ignore
async def test_store_credentials_failure(
    mock_dapr_output_client: MagicMock, config: Dict[str, Any]
) -> None:
    mock_dapr_output_client.reset_mock()  # Reset mock between examples
    mock_dapr_output_client.save_state.side_effect = Exception("Dapr error")

    with pytest.raises(Exception):
        await SecretStoreOutput.save_secret(config)
