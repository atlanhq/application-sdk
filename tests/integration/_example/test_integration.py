"""Example integration test class.

.. deprecated:: 3.58
   This example uses the ``BaseIntegrationTest`` HTTP scenario framework. It,
   and every name it is built from, are removed in v4.0.0 (FND-4659,
   FND-4911). Do not copy it for a new suite: use the in-process fixture kit
   described in ``docs/guides/integration-fixtures.md``.

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

import os

import pytest

from application_sdk.testing.integration import BaseIntegrationTest

from .scenarios import scenarios

# BaseIntegrationTest.setup_class loads .env, but the skip below is decided at
# import, before that runs. Load it here too (same call, same no-override
# semantics) so an app configured only in .env is not skipped.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # python-dotenv not installed; env vars come from the environment
    pass

# Skip, visibly and with a reason, until the app under test is configured.
# Without this guard a bare ``pytest`` from the repo root would run the class
# and fail its server health check on every machine that has no app running.
pytestmark = pytest.mark.skipif(
    not os.getenv("ATLAN_APPLICATION_NAME"),
    reason="ATLAN_APPLICATION_NAME is not set: no app under test to call",
)


# The ``Test`` prefix is load-bearing: pytest only collects classes whose
# name starts with ``Test`` (``python_classes``). A class named
# ``ExampleIntegrationTest`` collects zero tests and passes silently.
class TestExampleIntegration(BaseIntegrationTest):
    """Integration tests for the example connector.

    Just define scenarios - the framework handles everything:
    - Credentials auto-loaded from E2E_* env vars
    - Server URL auto-discovered from ATLAN_APP_HTTP_HOST/PORT
    - Individual pytest tests auto-generated per scenario
    - Rich assertion error messages
    """

    scenarios = scenarios
