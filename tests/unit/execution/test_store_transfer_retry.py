"""The store-transfer tasks must retry across a window, not inside a blink.

FND-2076: ``upload_refs`` failed a full-DAG run when a tenant's blobstorage
gateway rejected every signed request for ~55s. The task had three attempts,
but on the default 1-second initial interval it spent all three inside 25s and
gave up while the dependency was still recovering. What was short was the
*spread*, so that is what these pin: the initial interval reaching Temporal,
and the four framework store-transfer tasks declaring it.
"""

from datetime import timedelta

import pytest

from application_sdk.app.base import (
    _STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
    _STORE_TRANSFER_MAX_ATTEMPTS,
    _STORE_TRANSFER_MAX_INTERVAL_SECONDS,
)
from application_sdk.app.task import TaskMetadata, get_task_metadata
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.activities import get_activity_options


class _In(Input):
    pass


class _Out(Output):
    pass


def _meta(**overrides: object) -> TaskMetadata:
    base: dict[str, object] = {
        "name": "t",
        "app_name": "_a",
        "func": lambda: None,
        "input_type": _In,
        "output_type": _Out,
        "timeout_seconds": 60,
        "heartbeat_timeout_seconds": 60,
        "auto_heartbeat_seconds": 10,
        "retry_policy": None,
    }
    base.update(overrides)
    return TaskMetadata(**base)  # type: ignore[arg-type]


class TestRetryInitialInterval:
    def test_declared_initial_interval_reaches_temporal(self) -> None:
        opts = get_activity_options(_meta(retry_initial_interval_seconds=10))
        assert opts["retry_policy"].initial_interval == timedelta(seconds=10)

    def test_default_initial_interval_is_one_second(self) -> None:
        """The knob is additive: an undeclaring task keeps Temporal's default."""
        opts = get_activity_options(_meta())
        assert opts["retry_policy"].initial_interval == timedelta(seconds=1)

    def test_full_policy_still_wins_over_the_scalar(self) -> None:
        """``retry_policy`` takes precedence, as its docstring promises."""
        from application_sdk.execution.retry import RetryPolicy

        opts = get_activity_options(
            _meta(
                retry_initial_interval_seconds=10,
                retry_policy=RetryPolicy(
                    max_attempts=2, initial_interval=timedelta(seconds=3)
                ),
            )
        )
        assert opts["retry_policy"].initial_interval == timedelta(seconds=3)


class TestStoreTransferTasksWidenTheirWindow:
    """Every task that crosses a store boundary carries the wider shape.

    Named individually rather than swept, so adding a fifth store-transfer task
    is a deliberate decision to add it here too.
    """

    @pytest.mark.parametrize(
        "task_name", ["upload", "download", "verify_refs", "upload_refs"]
    )
    def test_task_declares_the_store_transfer_retry_shape(self, task_name) -> None:
        from application_sdk.app.base import App

        meta = get_task_metadata(getattr(App, task_name))
        assert meta is not None
        assert meta.retry_max_attempts == _STORE_TRANSFER_MAX_ATTEMPTS
        assert (
            meta.retry_initial_interval_seconds
            == _STORE_TRANSFER_INITIAL_INTERVAL_SECONDS
        )
        assert meta.retry_max_interval_seconds == _STORE_TRANSFER_MAX_INTERVAL_SECONDS

    def test_the_shape_outlasts_the_observed_outage(self) -> None:
        """The numbers are only right if the backoff sum clears the evidence.

        The FND-2076 gateway rejected every request from 14:30:01 to at least
        14:30:56 — 55 seconds. A policy whose backoff sums to less than that
        cannot ride it out, whatever the attempt count says.
        """
        interval = _STORE_TRANSFER_INITIAL_INTERVAL_SECONDS
        total = 0
        for _ in range(_STORE_TRANSFER_MAX_ATTEMPTS - 1):
            total += min(interval, _STORE_TRANSFER_MAX_INTERVAL_SECONDS)
            interval *= 2
        assert total > 55
