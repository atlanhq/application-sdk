"""The ``ApplicationError`` docstring documents exactly the parameters Temporal accepts."""

import inspect
import re

import pytest
from temporalio.exceptions import ApplicationError as TemporalApplicationError

from application_sdk.execution.errors import ApplicationError


def _documented_args(doc: str) -> set[str]:
    """Return parameter names listed in the ``Args:`` section of a docstring."""
    section = inspect.cleandoc(doc).split("Args:", 1)[1]
    return set(re.findall(r"^ {4}\*{0,2}(\w+):", section, flags=re.MULTILINE))


def test_documented_args_match_temporal_signature() -> None:
    params = inspect.signature(TemporalApplicationError.__init__).parameters
    accepted = {name for name in params if name != "self"}

    assert ApplicationError.__doc__ is not None
    assert _documented_args(ApplicationError.__doc__) == accepted


def test_cause_keyword_is_rejected_and_from_chains() -> None:
    with pytest.raises(TypeError):
        ApplicationError("boom", cause=ValueError("root"))  # type: ignore[call-arg]

    root = ValueError("root")
    with pytest.raises(ApplicationError) as excinfo:
        try:
            raise root
        except ValueError as exc:
            raise ApplicationError("boom", non_retryable=True) from exc
    assert excinfo.value.__cause__ is root
