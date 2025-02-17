import os
import sys
from unittest.mock import patch
import pytest
from application_sdk.config import ApplicationConfig

# Create test config instance
test_config = ApplicationConfig(
    sql_chunk_size=1000,
    json_chunk_size=1000,
    max_transform_concurrency=5,
    sql_use_server_side_cursor=True
)

# Start the patch before any test imports
_config_patcher = patch('application_sdk.config.config', test_config)
_config_patcher.start()

# Make the config available as a fixture
@pytest.fixture
def mock_config():
    """Fixture that provides access to the mocked config"""
    return test_config

# Clean up the patch after tests
def pytest_sessionfinish(session, exitstatus):
    _config_patcher.stop() 