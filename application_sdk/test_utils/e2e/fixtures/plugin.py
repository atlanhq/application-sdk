"""Pytest plugin for DataSourceFixture lifecycle management.

Apps activate this by setting ``_datasource_fixture_class`` on the pytest
config object in their own ``pytest_configure`` hook::

    def pytest_configure(config):
        config._datasource_fixture_class = MyFixture

The plugin is registered as a pytest11 entry point so it loads automatically.
It is a no-op when no fixture class is registered.
"""

import os
from typing import Optional, Type

import pytest

from application_sdk.test_utils.e2e.fixtures.base import DataSourceFixture

_active_fixture: Optional[DataSourceFixture] = None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Instantiate and start the data source fixture if one is registered."""
    global _active_fixture

    fixture_class: Optional[Type[DataSourceFixture]] = getattr(
        session.config, "_datasource_fixture_class", None
    )
    if fixture_class is None:
        return

    _active_fixture = fixture_class()
    connection_info = _active_fixture.setup()

    env_vars = _active_fixture.get_env_vars()
    for key, value in env_vars.items():
        os.environ[key] = value

    session.config._datasource_connection_info = connection_info  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tear down the data source fixture."""
    global _active_fixture

    if _active_fixture is not None:
        _active_fixture.teardown()
        _active_fixture = None
