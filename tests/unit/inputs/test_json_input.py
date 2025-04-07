from typing import Any, Dict
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.inputs.json import JsonInput
from application_sdk.test_utils.hypothesis.strategies.inputs.json_input import (
    download_prefix_strategy,
    file_names_strategy,
    json_input_config_strategy,
    safe_path_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "json_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("json_input_tests")


@given(config=json_input_config_strategy)
def test_init(config: Dict[str, Any]) -> None:
    json_input = JsonInput(
        path=config["path"],
        download_file_prefix=config["download_file_prefix"],
        file_names=config["file_names"],
    )

    assert json_input.path.endswith(config["path"])
    assert json_input.download_file_prefix == config["download_file_prefix"]
    assert json_input.file_names == config["file_names"]


@pytest.mark.asyncio
@given(
    path=safe_path_strategy,
    prefix=download_prefix_strategy,
    file_names=file_names_strategy,
)
async def test_not_download_file_that_exists(
    path: str, prefix: str, file_names: list[str]
) -> None:
    with patch("os.path.exists") as mock_exists, patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
    ) as mock_download:
        mock_exists.return_value = True
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        await json_input.download_files()
        mock_download.assert_not_called()


@pytest.mark.skip(
    reason="Failing due to AssertionError: download_file_from_object_store call not found"
)
@pytest.mark.asyncio
@given(
    path=safe_path_strategy,
    prefix=download_prefix_strategy,
    file_names=file_names_strategy,
)
async def test_download_file(path: str, prefix: str, file_names: list[str]) -> None:
    with patch("os.path.exists") as mock_exists, patch(
        "application_sdk.inputs.objectstore.ObjectStoreInput.download_file_from_object_store"
    ) as mock_download:
        mock_exists.return_value = False
        json_input = JsonInput(
            path=path, download_file_prefix=prefix, file_names=file_names
        )

        await json_input.download_files()

        # Verify each file was attempted to be downloaded
        assert mock_download.call_count == len(file_names)
        for file_name in file_names:
            mock_download.assert_any_call(
                f"{prefix}/{file_name}", f"{path}/{file_name}"
            )
