"""Pytest plugin for DataSourceFixture lifecycle management.

Apps activate this by setting ``_datasource_yaml`` on the pytest config
object in their own ``pytest_configure`` hook::

    def pytest_configure(config):
        config._datasource_yaml = "tests/e2e/datasource.yaml"

The plugin is registered as a pytest11 entry point so it loads automatically.
It is a no-op when no datasource YAML path is set.
"""

import os
from typing import Optional

import pytest

from application_sdk.test_utils.e2e.fixtures.base import DataSourceFixture

_active_fixture: Optional[DataSourceFixture] = None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Load datasource fixture from YAML and start it."""
    global _active_fixture

    yaml_path: Optional[str] = getattr(session.config, "_datasource_yaml", None)
    if yaml_path is None:
        return

    from application_sdk.test_utils.e2e.fixtures.loader import load_fixture_from_yaml

    _active_fixture = load_fixture_from_yaml(yaml_path)
    connection_info = _active_fixture.setup()

    for key, value in _active_fixture.get_env_vars().items():
        os.environ[key] = value

    session.config._datasource_connection_info = connection_info  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tear down the data source fixture."""
    global _active_fixture

    if _active_fixture is not None:
        _active_fixture.teardown()
        _active_fixture = None
