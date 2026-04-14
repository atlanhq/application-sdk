"""M: Multi-entry-point (@entrypoint) integration tests.

Verifies that Apps with multiple @entrypoint methods:
  - Dispatch to the correct method when entry_point is specified
  - Propagate NonRetryableError as non-retryable ApplicationError
  - Call on_complete() after both success and failure

Requires a running Temporal dev server (see conftest.py).
"""

import pytest

from application_sdk.app.base import App, NonRetryableError
from application_sdk.app.context import AppContext
from application_sdk.app.entrypoint import entrypoint
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY, RetryPolicy

# ---------------------------------------------------------------------------
# All App/Input/Output classes at module level so Temporal's sandboxed runner
# can import them by module path.  clean_registries (autouse in conftest)
# resets AppRegistry/TaskRegistry before each test; each test calls
# reregister_app() to restore entries before spinning up the worker.
# ---------------------------------------------------------------------------

# --- Dispatch ---


class ExtractInput(Input):
    source: str = ""


class ExtractOutput(Output):
    rows: int = 0


class MineInput(Input):
    query: str = ""


class MineOutput(Output):
    count: int = 0


class DispatchApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        return ExtractOutput(rows=len(input.source))

    @entrypoint
    async def mine_queries(self, input: MineInput) -> MineOutput:
        return MineOutput(count=len(input.query))


# --- NonRetryableError ---


class NRMultiInput(Input):
    pass


class NRMultiOutput(Output):
    pass


class NRMultiApp(App):
    @entrypoint
    async def fail_hard(self, input: NRMultiInput) -> NRMultiOutput:
        raise NonRetryableError("multi-ep non-retryable")

    @entrypoint
    async def succeed(self, input: NRMultiInput) -> NRMultiOutput:
        return NRMultiOutput()


# --- on_complete lifecycle ---

_lifecycle_log: list[str] = []


class LifecycleInput(Input):
    pass


class LifecycleOutput(Output):
    pass


class LifecycleMultiApp(App):
    async def on_complete(self) -> None:
        _lifecycle_log.append("called")

    @task
    async def do_work(self, input: LifecycleInput) -> LifecycleOutput:
        return LifecycleOutput()

    @entrypoint
    async def run_a(self, input: LifecycleInput) -> LifecycleOutput:
        return await self.do_work(input)

    @entrypoint
    async def run_fail(self, input: LifecycleInput) -> LifecycleOutput:
        raise ValueError("intentional failure in run_fail")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_extract_entry_point_dispatches_correctly(
    run_worker, executor, reregister_app
):
    """M1.1: extract entry point returns ExtractOutput with correct row count."""
    reregister_app(DispatchApp)
    async with run_worker():
        context = AppContext(app_name=DispatchApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            DispatchApp,
            ExtractInput(source="abc"),
            context=context,
            retry_policy=NO_RETRY,
            entry_point="extract",
        )
    assert result.rows == 3


@pytest.mark.integration
async def test_mine_queries_entry_point_dispatches_correctly(
    run_worker, executor, reregister_app
):
    """M1.2: mine_queries entry point returns MineOutput with correct count."""
    reregister_app(DispatchApp)
    async with run_worker():
        context = AppContext(app_name=DispatchApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            DispatchApp,
            MineInput(query="SELECT 1"),
            context=context,
            retry_policy=NO_RETRY,
            entry_point="mine-queries",
        )
    assert result.count == 8  # len("SELECT 1")


@pytest.mark.integration
async def test_entry_points_are_independent(run_worker, executor, reregister_app):
    """M1.3: Two entry points on the same app execute independently."""
    reregister_app(DispatchApp)
    async with run_worker():
        context = AppContext(app_name=DispatchApp._app_name, app_version="1.0.0")
        extract_result = await executor.execute(
            DispatchApp,
            ExtractInput(source="hello"),
            context=context,
            retry_policy=NO_RETRY,
            entry_point="extract",
        )
        mine_result = await executor.execute(
            DispatchApp,
            MineInput(query="hi"),
            context=context,
            retry_policy=NO_RETRY,
            entry_point="mine-queries",
        )
    assert extract_result.rows == 5
    assert mine_result.count == 2


@pytest.mark.integration
async def test_non_retryable_error_from_entrypoint_app(
    run_worker, executor, reregister_app
):
    """M2.1: NonRetryableError raised inside a @entrypoint method propagates as non-retryable."""
    reregister_app(NRMultiApp)
    async with run_worker():
        context = AppContext(app_name=NRMultiApp._app_name, app_version="1.0.0")
        with pytest.raises(Exception) as exc_info:
            await executor.execute(
                NRMultiApp,
                NRMultiInput(),
                context=context,
                retry_policy=RetryPolicy(max_attempts=5),
                entry_point="fail-hard",
            )

    cause: BaseException | None = exc_info.value
    while cause is not None:
        if "multi-ep non-retryable" in str(cause):
            break
        cause = getattr(cause, "cause", None) or cause.__cause__
    assert cause is not None, "Expected 'multi-ep non-retryable' in exception chain"


@pytest.mark.integration
async def test_on_complete_called_after_entrypoint_success(
    run_worker, executor, reregister_app
):
    """M3.1: on_complete() fires after a successful @entrypoint execution."""
    _lifecycle_log.clear()
    reregister_app(LifecycleMultiApp)
    async with run_worker():
        context = AppContext(app_name=LifecycleMultiApp._app_name, app_version="1.0.0")
        await executor.execute(
            LifecycleMultiApp,
            LifecycleInput(),
            context=context,
            retry_policy=NO_RETRY,
            entry_point="run-a",
        )
    assert _lifecycle_log == ["called"]


@pytest.mark.integration
async def test_on_complete_called_after_entrypoint_failure(
    run_worker, executor, reregister_app
):
    """M3.2: on_complete() fires even when a @entrypoint method raises."""
    _lifecycle_log.clear()
    reregister_app(LifecycleMultiApp)
    async with run_worker():
        context = AppContext(app_name=LifecycleMultiApp._app_name, app_version="1.0.0")
        with pytest.raises(Exception):
            await executor.execute(
                LifecycleMultiApp,
                LifecycleInput(),
                context=context,
                retry_policy=NO_RETRY,
                entry_point="run-fail",
            )
    assert _lifecycle_log == ["called"]
