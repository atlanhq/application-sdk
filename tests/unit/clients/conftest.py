"""Test configuration for the SQL client tests.

``install_tolerant_connection_decoder`` rewrites ``psycopg2.extensions.encodings``
in place — deliberately, because that dict is the only Python-reachable seam for
psycopg2's connection decoder (see ``application_sdk.clients.sql_typecasters``).
The mutation is process-global, so any test that builds a SQL engine leaks it
into every test that runs afterwards.

That is fine in a connector worker and not fine in a test session: it would make
``TestRealPsycopg2Premises`` — the class that pins the driver facts the fix
depends on — pass or fail depending on which tests ran before it. This fixture
puts the map back after every test in this directory, so the global behaviour
stays exercised while the ordering coupling goes away.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator

import pytest

# Snapshot at conftest import time: collection happens before any test runs, so
# this is the driver's pristine mapping.
if importlib.util.find_spec("psycopg2") is not None:
    import psycopg2.extensions as _psycopg2_ext

    _PRISTINE_PSYCOPG2_ENCODINGS: dict[str, str] | None = dict(_psycopg2_ext.encodings)
else:  # psycopg2 is a test-only dep; tolerate interpreters without a wheel
    _psycopg2_ext = None  # type: ignore[assignment]  # — sentinel for "driver not installed": None is not a module
    _PRISTINE_PSYCOPG2_ENCODINGS = None


@pytest.fixture(autouse=True)
def restore_psycopg2_encodings() -> Iterator[None]:
    """Undo any process-global rewrite of psycopg2's encodings map."""
    yield
    if _PRISTINE_PSYCOPG2_ENCODINGS is None or _psycopg2_ext is None:
        return
    if _psycopg2_ext.encodings != _PRISTINE_PSYCOPG2_ENCODINGS:
        _psycopg2_ext.encodings.clear()
        _psycopg2_ext.encodings.update(_PRISTINE_PSYCOPG2_ENCODINGS)
