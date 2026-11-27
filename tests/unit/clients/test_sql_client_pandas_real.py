"""The pandas read path, against real pandas and sqlalchemy.

``_execute_pandas_query`` hands pandas a ``TextClause`` and the SQLAlchemy
connection, with no fallback to the raw DBAPI connection. pandas only takes its
SQLAlchemy path when the installed sqlalchemy meets pandas' own minimum, so the
SDK's sqlalchemy floor must stay at or above that minimum.
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import sqlalchemy as sa
from packaging.requirements import Requirement
from packaging.version import Version
from sqlalchemy.sql.elements import TextClause

from application_sdk.clients.sql import BaseSQLClient

ROOT = Path(__file__).resolve().parents[3]
PYPROJECTS = (ROOT / "pyproject.toml", ROOT / "packages" / "api" / "pyproject.toml")


@pytest.fixture
def engine() -> Iterator[sa.engine.Engine]:
    engine = sa.create_engine("sqlite://")
    yield engine
    engine.dispose()


def test_read_sql_query_receives_a_text_clause_and_the_sqlalchemy_connection(
    engine: sa.engine.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, Any]] = []
    real_read_sql_query = pd.read_sql_query

    def spy(sql: Any, con: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((sql, con))
        return real_read_sql_query(sql, con, *args, **kwargs)

    monkeypatch.setattr(pd, "read_sql_query", spy)
    with engine.connect() as conn:
        df = BaseSQLClient()._execute_pandas_query(
            conn, "SELECT '12:30:00' AS t", chunksize=None
        )
    assert df.to_dict("records") == [{"t": "12:30:00"}]
    [(sql, con)] = calls
    assert isinstance(sql, TextClause)
    assert con is conn


def _min_version(requirement: Requirement) -> Version:
    floors = [Version(s.version) for s in requirement.specifier if s.operator == ">="]
    assert len(floors) == 1, f"{requirement} has no single >= floor"
    return floors[0]


def _pandas_sqlalchemy_minimum() -> Version:
    """pandas' own sqlalchemy minimum, from its public distribution metadata."""
    for raw in importlib.metadata.requires("pandas") or []:
        requirement = Requirement(raw)
        if requirement.name.lower() == "sqlalchemy" and (
            requirement.marker is None
            or requirement.marker.evaluate({"extra": "sql-other"})
        ):
            return _min_version(requirement)
    raise AssertionError("pandas declares no sqlalchemy requirement")


def _sdk_sqlalchemy_floors() -> dict[str, Version]:
    floors: dict[str, Version] = {}
    for path in PYPROJECTS:
        project = tomllib.loads(path.read_text())["project"]
        groups = {"dependencies": project.get("dependencies", [])}
        groups.update(project.get("optional-dependencies", {}))
        for group, requirements in groups.items():
            for raw in requirements:
                requirement = Requirement(raw)
                if requirement.name.lower() == "sqlalchemy":
                    floors[f"{path.relative_to(ROOT)} [{group}]"] = _min_version(
                        requirement
                    )
    return floors


def test_the_sdk_sqlalchemy_floor_meets_the_installed_pandas_minimum() -> None:
    minimum = _pandas_sqlalchemy_minimum()
    floors = _sdk_sqlalchemy_floors()
    assert floors, "no sqlalchemy requirement found in the SDK's pyproject files"
    below = {where: str(v) for where, v in floors.items() if v < minimum}
    assert not below, (
        f"pandas {pd.__version__} needs sqlalchemy>={minimum} for its SQLAlchemy "
        f"path; raise these floors: {below}"
    )
