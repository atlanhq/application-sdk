"""Example integration test class.

This file demonstrates how to create an integration test class
using the Apps-SDK integration testing framework.

Prerequisites:
    1. Set environment variables:
        ATLAN_APPLICATION_NAME=your_app
        E2E_YOUR_APP_HOST=localhost
        E2E_YOUR_APP_USERNAME=test
        E2E_YOUR_APP_PASSWORD=secret

    2. Start application server:
        uv run python main.py

    3. Run tests:
        pytest tests/integration/ -v
        pytest tests/integration/ -v -k "auth_valid"
"""

from application_sdk.test_utils.integration import BaseIntegrationTest

from .scenarios import scenarios


class ExampleIntegrationTest(BaseIntegrationTest):
    """Integration tests for the example connector.

    Just define scenarios - the framework handles everything:
    - Credentials auto-loaded from E2E_* env vars
    - Server URL auto-discovered from ATLAN_APP_HTTP_HOST/PORT
    - Individual pytest tests auto-generated per scenario
    - Rich assertion error messages
    """

    scenarios = scenarios
