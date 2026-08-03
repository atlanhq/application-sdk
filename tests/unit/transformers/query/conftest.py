"""Shared inputs for the transformer tests that run a real transform.

These are exposed as fixtures rather than importable constants so both modules
read one source without depending on pytest's sys.path insertion.
"""

from typing import Any

import pytest

from application_sdk.transformers.query import QueryBasedTransformer


@pytest.fixture
def postgres_transformer() -> QueryBasedTransformer:
    return QueryBasedTransformer(connector_name="postgres", tenant_id="default")


@pytest.fixture
def transform_args() -> dict[str, Any]:
    """Workflow identity and connection every real transform here runs under."""
    return {
        "workflow_id": "79a40801-07c2-4852-86c4-9703bda3a840",
        "workflow_run_id": "019667f9-31e9-77b0-b7c0-b901bd30d140",
        "connection_qualified_name": "default/postgres/1745501106",
        "connection_name": "dev",
    }
