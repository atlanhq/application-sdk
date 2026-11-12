"""FND-4963: ``RecordSkipped``, a sync mapper's hand-off to ``skip_record``.

The end-to-end path (mapper raises, loop calls ``skip_record``, the artifact,
metric and ``PARTIAL_SUCCESS`` reach the workflow output, a creator's
``ValueError`` is not caught) is in
``tests/unit/execution/_temporal/interceptors/test_output_interceptor.py``.
"""

from __future__ import annotations

import pickle
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors import AppError
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.outputs import (
    BlankIdentity,
    MalformedRecord,
    MissingRequiredField,
    RecordSkipped,
    SkipReason,
    UnsupportedType,
)

_SECRET = "customer-value-must-not-leak"
_RECORD = {"id": "t1", "schema": "", "payload": _SECRET}


def _skip() -> BlankIdentity:
    return BlankIdentity(
        _RECORD, record_id="t1", entity_type="Table", detail=f"detail {_SECRET}"
    )


class TestShape:
    def test_one_leaf_per_reason(self):
        leaves = RecordSkipped.__subclasses__()
        assert {leaf.reason for leaf in leaves} == set(SkipReason)
        assert len(leaves) == len(SkipReason)
        assert (
            BlankIdentity.reason,
            MissingRequiredField.reason,
            MalformedRecord.reason,
            UnsupportedType.reason,
        ) == (
            SkipReason.BLANK_IDENTITY,
            SkipReason.MISSING_REQUIRED_FIELD,
            SkipReason.MALFORMED_RECORD,
            SkipReason.UNSUPPORTED_TYPE,
        )

    def test_base_cannot_be_raised(self):
        with pytest.raises(TypeError, match="leaf"):
            RecordSkipped(_RECORD, record_id="t1", entity_type="Table")

    def test_carries_everything_skip_record_needs(self):
        skip = _skip()
        assert skip.record is _RECORD
        assert (skip.reason, skip.record_id, skip.entity_type, skip.detail) == (
            SkipReason.BLANK_IDENTITY,
            "t1",
            "Table",
            f"detail {_SECRET}",
        )

    def test_is_control_flow_not_an_app_error(self):
        assert not issubclass(RecordSkipped, AppError)
        assert issubclass(RecordSkipped, Exception)

    def test_record_and_detail_stay_out_of_text(self):
        skip = _skip()
        try:
            raise skip
        except BlankIdentity:
            rendered = traceback.format_exc()
        for text in (str(skip), repr(skip), repr(skip.args), rendered):
            assert _SECRET not in text
            assert "payload" not in text
        assert str(skip) == "Table t1 skipped: blank_identity"

    def test_survives_pickling(self):
        clone = pickle.loads(pickle.dumps(_skip()))
        assert type(clone) is BlankIdentity
        assert clone.record == _RECORD
        assert (clone.record_id, clone.entity_type, clone.detail) == (
            "t1",
            "Table",
            f"detail {_SECRET}",
        )


class _In(Input, allow_unbounded_fields=True):
    name: str = "x"


class _Out(Output, allow_unbounded_fields=True):
    msg: str = ""


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()


@pytest.fixture
def _no_infrastructure() -> Any:
    with (
        mock.patch.object(
            activities_module.activity,
            "info",
            return_value=mock.MagicMock(workflow_id="wf-skip"),
        ),
        mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=None,
        ),
    ):
        yield


@pytest.mark.usefixtures("_no_infrastructure")
async def test_uncaught_skip_fails_the_activity_non_retryably():
    """A @task that forgets the except fails visibly, once, without the record."""

    class _ForgetfulApp(App):
        @task(timeout_seconds=60)
        async def transform(self, input: _In) -> _Out:
            raise _skip()

        async def run(self, input: _In) -> _Out:
            return await self.transform(input)

    (task_meta,) = [
        t
        for t in TaskRegistry.get_instance().get_tasks_for_app("_forgetful-app")
        if t.name == "transform"
    ]
    activity_fn = cast(
        "Callable[[TaskContext, _In], Awaitable[_Out]]",
        create_activity_from_task(task_meta),
    )
    context = TaskContext(
        app_name="_forgetful-app",
        task_name="transform",
        run_id="run-1",
        heartbeat_timeout_seconds=None,
        auto_heartbeat_seconds=None,
    )

    with pytest.raises(ApplicationError) as exc_info:
        await activity_fn(context, _In())

    error = exc_info.value
    assert error.type == "InternalError"
    assert error.non_retryable is True
    assert "Table t1 skipped: blank_identity" in str(error)
    assert "skip_record" in str(error)
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error.details)
