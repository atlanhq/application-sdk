"""The store-transfer tasks must retry across a window, not inside a blink.

FND-2076: ``upload_refs`` failed a full-DAG run when a tenant's blobstorage
gateway rejected every signed request for ~55s. The task had three attempts,
but on the default 1-second initial interval it spent all three inside 25s and
gave up while the dependency was still recovering. What was short was the
*spread*, so that is what these pin: the initial interval reaching Temporal,
and the four framework store-transfer tasks declaring it.

The interval is asserted on **both** dispatch paths —
``_create_task_activity_wrapper`` (the workflow-side wrapper
``_wrap_instance_tasks`` installs, and the one that actually calls
``execute_activity_with_eviction_retry``) and ``get_activity_options``. The
first version of this fix plumbed only the second, so the widened spread never
reached Temporal on the path every instance task takes; a declaration that
resolves correctly but never reaches the wire has to fail here.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app.base import (
    _STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
    _STORE_TRANSFER_MAX_ATTEMPTS,
    _STORE_TRANSFER_MAX_INTERVAL_SECONDS,
    _wrap_instance_tasks,
)
from application_sdk.app.task import TaskMetadata, get_task_metadata
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.activities import get_activity_options

_EVICTION_RETRY_PATH = (
    "application_sdk.execution._temporal.eviction_retry."
    "execute_activity_with_eviction_retry"
)


class _In(Input):
    pass


class _Out(Output):
    pass


async def _wrapper_dispatch_kwargs(**wrapper_kwargs: object) -> dict[str, object]:
    """Kwargs the workflow-side wrapper hands Temporal for one dispatch."""
    with patch(_EVICTION_RETRY_PATH, new_callable=AsyncMock) as mock_exec:
        from application_sdk.app.base import _create_task_activity_wrapper

        mock_exec.return_value = MagicMock()
        wrapper = _create_task_activity_wrapper(
            app_name="_a",
            task_name="t",
            output_type=_Out,
            context_data={"run_id": "r1", "correlation_id": "c1"},
            timeout_seconds=60,
            retry_max_attempts=3,
            retry_max_interval_seconds=30,
            **wrapper_kwargs,  # type: ignore[arg-type]
        )
        await wrapper(MagicMock())

    return dict(mock_exec.call_args.kwargs)


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


class TestRetryInitialIntervalOnTheWrapperPath:
    """The same assertions, through the dispatch path instance tasks take.

    ``get_activity_options`` is not the path a `@task` method on an App
    instance goes through — ``_wrap_instance_tasks`` builds a
    ``_create_task_activity_wrapper`` closure instead, and that closure builds
    its own ``RetryPolicy``. Pinning only the first leaves the second free to
    silently drop the interval, which is exactly what it did.
    """

    async def test_declared_initial_interval_reaches_temporal(self) -> None:
        kwargs = await _wrapper_dispatch_kwargs(retry_initial_interval_seconds=10)
        assert kwargs["retry_policy"].initial_interval == timedelta(seconds=10)

    async def test_default_initial_interval_is_one_second(self) -> None:
        kwargs = await _wrapper_dispatch_kwargs()
        assert kwargs["retry_policy"].initial_interval == timedelta(seconds=1)

    async def test_full_policy_still_wins_over_the_scalar(self) -> None:
        from application_sdk.execution.retry import RetryPolicy

        kwargs = await _wrapper_dispatch_kwargs(
            retry_initial_interval_seconds=10,
            retry_policy=RetryPolicy(
                max_attempts=2, initial_interval=timedelta(seconds=3)
            ),
        )
        assert kwargs["retry_policy"].initial_interval == timedelta(seconds=3)

    async def test_wrap_instance_tasks_forwards_the_declaration(self) -> None:
        """End to end: `@task` declaration → wrapper → Temporal.

        The seam the first fix missed. Nothing is passed by hand here — the
        value has to survive ``TaskMetadata`` and ``_wrap_instance_tasks``'s
        long positional call to reach the wire.
        """
        from application_sdk.app.base import App
        from application_sdk.contracts.storage import UploadRefsInput

        class _Probe(App):
            pass

        instance = _Probe.__new__(_Probe)
        instance._app_name = "_a"

        with patch(_EVICTION_RETRY_PATH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = MagicMock()
            _wrap_instance_tasks(instance, {"run_id": "r1", "correlation_id": "c1"})
            await instance.upload_refs(UploadRefsInput(files=[]))

        policy = mock_exec.call_args.kwargs["retry_policy"]
        assert policy.initial_interval == timedelta(
            seconds=_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS
        )
        assert policy.maximum_attempts == _STORE_TRANSFER_MAX_ATTEMPTS
        assert policy.maximum_interval == timedelta(
            seconds=_STORE_TRANSFER_MAX_INTERVAL_SECONDS
        )


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
