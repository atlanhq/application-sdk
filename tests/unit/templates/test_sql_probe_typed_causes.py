"""SQL probes retain connector verdicts even when driver causes are attached."""

from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import DBAPIError

from application_sdk.clients.sql_errors import SqlPandasResultError
from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InternalError,
    SourceUnavailableError,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    PrimeAuthOutput,
)
from application_sdk.templates.sql_app import SqlApp


@dataclass(kw_only=True)
class MssqlSourceBusyError(DependencyUnavailableError):
    """Representative connector leaf, without importing a consumer app."""

    code: ClassVar[str] = "MSSQL_SOURCE_BUSY"
    audience: ClassVar[Audience] = Audience.USER


class Error(Exception):
    """Driver-shaped exception; no native ODBC dependency is needed."""


def _driver_chain(number: int) -> DBAPIError:
    """Build the SQLAlchemy-to-driver cause chain seen on SQL connect failures."""
    driver = Error(f"HY000 [SQL Server]Database unavailable ({number})")
    dbapi = DBAPIError(None, None, driver)
    dbapi.__cause__ = driver
    return dbapi


@pytest.mark.parametrize("number", [40613, 10928, 10929])
def test_connector_busy_verdict_survives_driver_cause(number: int):
    """Typed retryability wins over an unclassified raw driver message."""
    typed = MssqlSourceBusyError(
        message="Source busy: PWD=SyntheticProbeSecret",
        suggested_action="Retry; PWD=SyntheticActionSecret",
        network_error="PWD=SyntheticEvidenceSecret",
        cause=_driver_chain(number),
    )
    wrapper = SqlPandasResultError(message="SQL result failed", cause=typed)

    classified = SqlApp._classify_probe_exception(wrapper)

    assert classified is typed
    assert classified.effective_retryable is True
    assert classified.audience is Audience.USER
    wire = classified.to_failure_details().model_dump_json()
    assert "SyntheticProbeSecret" not in wire
    assert "SyntheticActionSecret" not in wire
    assert "SyntheticEvidenceSecret" not in wire
    assert "PWD=***" in wire


@pytest.mark.parametrize("leaf", [AuthError, SourceUnavailableError])
def test_outermost_connector_verdict_wins(leaf: type[AppError]):
    """An explicit connector verdict takes precedence over a deeper typed leaf."""
    inner = InternalError(message="lower-level failure", cause=Error("unknown"))
    typed = leaf(message="connector verdict", cause=inner)
    wrapper = RuntimeError("wrapper")
    wrapper.__cause__ = typed

    assert SqlApp._classify_probe_exception(wrapper) is typed


async def test_prime_serializes_retryable_connector_verdict():
    """The prime activity's failure envelope keeps the connector's retry policy."""
    typed = MssqlSourceBusyError(message="Source busy", cause=_driver_chain(40613))
    client = AsyncMock()
    client.get_results.side_effect = SqlPandasResultError(
        message="SQL result failed", cause=typed
    )
    app = SqlApp.__new__(SqlApp)
    app._app_name = "test-app"
    input = ExtractionTaskInput(
        workflow_id="wf-test",
        output_path="/tmp/test",
        output_prefix="/tmp",
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
    )
    with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=client)):
        result = await app.prime_sql_auth(input)

    rehydrated = PrimeAuthOutput.model_validate_json(result.model_dump_json())
    assert rehydrated.success is False
    assert rehydrated.failure is not None
    assert rehydrated.failure.code == typed.code
    assert rehydrated.failure.retryable is True
    assert rehydrated.failure.audience is Audience.USER
    assert rehydrated.error_type == "Error"
    client.close.assert_awaited_once()


def test_untyped_cause_cycle_falls_back_without_hanging():
    """A malformed chain still terminates and uses the existing root fallback."""
    outer = RuntimeError("outer")
    inner = Error("unknown driver failure")
    outer.__cause__ = inner
    inner.__cause__ = outer

    assert isinstance(SqlApp._classify_probe_exception(outer), InternalError)


def test_typed_cause_cycle_keeps_connector_verdict():
    """A typed verdict remains authoritative even on a malformed cause chain."""
    typed = MssqlSourceBusyError(message="Source busy")
    driver = Error("unknown driver failure")
    typed.__cause__ = driver
    driver.__cause__ = typed

    assert SqlApp._classify_probe_exception(typed) is typed
