"""P2: Error handling and retry tests.

Verifies that NonRetryableError stops immediately, retryable errors are
retried correctly, and custom RetryPolicy settings are honoured.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from application_sdk.app.base import App, NonRetryableError
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY, RetryPolicy

# Module-level counter dict — shared between test thread and worker thread
# (because passthrough_modules={"tests"} disables sandbox for this module).
_attempt_counters: dict[str, int] = {}


@pytest.mark.integration
async def test_non_retryable_error_stops_immediately(run_worker, executor):
    """P2.1: NonRetryableError propagates as non-retryable ApplicationError."""

    @dataclass
    class NRInput(Input):
        pass

    @dataclass
    class NROutput(Output):
        pass

    class NonRetryableApp(App):
        @task
        async def do_work(self, input: NRInput) -> NROutput:
            raise NonRetryableError("Deterministic failure — do not retry")

        async def run(self, input: NRInput) -> NROutput:
            return await self.do_work(input)

    async with run_worker():
        context = AppContext(app_name=NonRetryableApp._app_name, app_version="1.0.0")
        with pytest.raises(Exception, match="Deterministic failure"):
            await executor.execute(
                NonRetryableApp,
                NRInput(),
                context=context,
                retry_policy=RetryPolicy(max_attempts=5),
            )


@pytest.mark.integration
async def test_retryable_error_succeeds_on_third_attempt(run_worker, executor):
    """P2.2: Task with retry_max_attempts=3 retries twice and succeeds on third."""
    test_id = uuid4().hex
    _attempt_counters[test_id] = 0

    @dataclass
    class RetryInput(Input):
        test_id: str = ""

    @dataclass
    class RetryOutput(Output):
        attempts: int = 0

    class RetryApp(App):
        @task(retry_max_attempts=3, retry_max_interval_seconds=1)
        async def do_work(self, input: RetryInput) -> RetryOutput:
            _attempt_counters[input.test_id] = (
                _attempt_counters.get(input.test_id, 0) + 1
            )
            if _attempt_counters[input.test_id] < 3:
                raise ValueError("Not ready yet — will retry")
            return RetryOutput(attempts=_attempt_counters[input.test_id])

        async def run(self, input: RetryInput) -> RetryOutput:
            return await self.do_work(input)

    async with run_worker():
        context = AppContext(app_name=RetryApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            RetryApp,
            RetryInput(test_id=test_id),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.attempts == 3


@pytest.mark.integration
async def test_custom_retry_policy_propagation(run_worker, executor):
    """P2.3: Custom RetryPolicy with max_attempts=2 retries once and succeeds."""
    test_id = uuid4().hex
    _attempt_counters[test_id] = 0

    @dataclass
    class CustomRetryInput(Input):
        test_id: str = ""

    @dataclass
    class CustomRetryOutput(Output):
        attempts: int = 0

    from datetime import timedelta

    custom_policy = RetryPolicy(
        max_attempts=2,
        initial_interval=timedelta(milliseconds=100),
        max_interval=timedelta(seconds=1),
    )

    class CustomRetryApp(App):
        @task(retry_policy=custom_policy)
        async def do_work(self, input: CustomRetryInput) -> CustomRetryOutput:
            _attempt_counters[input.test_id] = (
                _attempt_counters.get(input.test_id, 0) + 1
            )
            if _attempt_counters[input.test_id] < 2:
                raise ValueError("First attempt fails")
            return CustomRetryOutput(attempts=_attempt_counters[input.test_id])

        async def run(self, input: CustomRetryInput) -> CustomRetryOutput:
            return await self.do_work(input)

    async with run_worker():
        context = AppContext(app_name=CustomRetryApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            CustomRetryApp,
            CustomRetryInput(test_id=test_id),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.attempts == 2
