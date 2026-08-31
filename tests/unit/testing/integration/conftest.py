"""Kit fixtures under test, imported the way an adopting conftest imports them.

Importing them here rather than in the test module keeps the fixture name from
shadowing the test parameter of the same name (ruff F811).
"""

from application_sdk.testing.integration.fixtures import (  # noqa: F401
    integration_options,
    temporary_path,
)
