"""App module - core developer-facing abstractions for building Apps.

Usage::

    from application_sdk.app import App, task, Input, Output

    @dataclass
    class MyInput(Input):
        source: str

    @dataclass
    class MyOutput(Output):
        count: int

    class MyApp(App):
        @task(timeout_seconds=300)
        async def fetch_data(self, input: MyInput) -> MyOutput:
            return MyOutput(count=42)

        async def run(self, input: MyInput) -> MyOutput:
            return await self.fetch_data(input)

Runtime interaction decorators and primitives are also exported here so app code
never needs to import the underlying orchestrator directly::

    from application_sdk.app import App, signal, query, update, wait_condition

    class MyApp(App):
        async def run(self, input: MyInput) -> MyOutput:
            await wait_condition(lambda: self.ready)
            ...

        @signal
        async def unblock(self) -> None:
            self.ready = True

        @query
        def status(self) -> StatusOutput:
            return StatusOutput(state=self._state)

        @update
        async def pause(self, input: PauseInput) -> PauseOutput:
            self._state = "paused"
            return PauseOutput(message=f"paused: {input.reason}")
"""

from temporalio.workflow import HandlerUnfinishedPolicy as _HandlerUnfinishedPolicy
from temporalio.workflow import now as _now
from temporalio.workflow import query as _query
from temporalio.workflow import signal as _signal
from temporalio.workflow import sleep as _sleep
from temporalio.workflow import update as _update
from temporalio.workflow import uuid4 as _uuid4
from temporalio.workflow import wait_condition as _wait_condition

from application_sdk.app.base import App, AppError, NonRetryableError, RetryableError
from application_sdk.app.context import AppContext
from application_sdk.app.entrypoint import EntryPointMetadata, entrypoint
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import TaskMetadata, task
from application_sdk.contracts.base import Input, Output, OutputStatus
from application_sdk.credentials.atlan_client import AtlanClientMixin
from application_sdk.execution.retry import RetryPolicy
from application_sdk.server.mcp.decorators import mcp_tool

# ---------------------------------------------------------------------------
# Runtime interaction decorators — re-exported with SDK-context docstrings.
# Variable docstrings (string literal after assignment) let griffe surface
# these in the capability manifest without needing wrapper functions.
# ---------------------------------------------------------------------------

signal = _signal
"""Declare a ``@signal`` runtime interaction on an App subclass.

A signal is a **pure trigger** — it carries no payload and returns nothing.
The caller fires and forgets; there is no typed confirmation.

**SDK contract** (enforced at ``generate_workflow_class`` time):

* **0 parameters** besides ``self``.
* Return type is ``None``.

To pass data into a running app or receive a response, use ``@update`` instead.

Example::

    from application_sdk.app import App, signal

    class ExtractApp(App):
        def __init__(self) -> None:
            self.paused: bool = False

        @signal
        async def resume(self) -> None:
            self.paused = False
"""

query = _query
"""Declare a ``@query`` runtime interaction that reads live state without mutation.

A query is a **read-only probe** — the caller receives a typed snapshot of
instance state. The interaction body must not mutate ``self``.

**SDK contract** (enforced at ``generate_workflow_class`` time):

* **0 parameters** besides ``self`` (no ``Input`` — queries carry no payload).
* Return type must be a subclass of ``Output``.

Example::

    from application_sdk.app import App, query
    from application_sdk.contracts.base import Output

    class ProgressOutput(Output):
        tables_done: int = 0
        tables_total: int = 0

    class ExtractApp(App):
        @query
        def progress(self) -> ProgressOutput:
            return ProgressOutput(
                tables_done=self.done, tables_total=self.total
            )
"""

update = _update
"""Declare a ``@update`` runtime interaction that mutates state and returns a typed response.

An update is delivered **exactly once** and the caller waits synchronously for
the response. Pair with ``@<method>.validator`` to validate input before the body
runs — a validator failure raises ``WorkflowUpdateFailedError`` on the client and
the body never executes.

**SDK contract** (enforced at ``generate_workflow_class`` time):

* **Exactly 1 parameter** besides ``self``, which must be a subclass of ``Input``.
* Return type must be a subclass of ``Output``.

Unlike ``@signal`` (no payload, no response) and ``@query`` (no payload, read-only
response), ``@update`` is the full request/response primitive: one typed ``Input``
in, one typed ``Output`` back.

Example::

    from application_sdk.app import App, update
    from application_sdk.contracts.base import Input, Output

    class PauseInput(Input):
        reason: str = ""

    class PauseOutput(Output):
        message: str = ""

    class ExtractApp(App):
        @update
        async def pause(self, input: PauseInput) -> PauseOutput:
            self.state = "paused"
            return PauseOutput(message=f"paused: {input.reason}")

        @pause.validator
        def _validate_pause(self, input: PauseInput) -> None:
            if not input.reason:
                raise ValueError("reason must be non-empty")
"""

# ---------------------------------------------------------------------------
# App-run time / UUID / sleep / condition primitives
# ---------------------------------------------------------------------------

wait_condition = _wait_condition
"""Suspend ``run()`` or a runtime interaction until a predicate becomes ``True``.

Prefer this over polling with ``asyncio.sleep`` — it is deterministic,
replay-safe, and incurs no cost while the condition is false.

Example::

    from application_sdk.app import wait_condition
    from datetime import timedelta

    # Block until an @update flips self.paused to False (5-minute timeout).
    await wait_condition(lambda: not self.paused, timeout=timedelta(minutes=5))
"""

now = _now
"""Return the current time from the app run's perspective (deterministic).

Use this instead of ``datetime.now()`` or ``time.time()`` inside ``run()``
and runtime interactions — those are non-deterministic and break replay.

Example::

    from application_sdk.app import now
    elapsed = (now() - self.started_at).total_seconds()
"""

sleep = _sleep
"""Sleep for a given duration inside an app run (deterministic).

Use this instead of ``asyncio.sleep`` or ``time.sleep`` inside ``run()``
and runtime interactions — those are non-deterministic and break replay.

Example::

    from application_sdk.app import sleep
    from datetime import timedelta

    await sleep(timedelta(milliseconds=100))  # yield between batches
"""

uuid4 = _uuid4
"""Generate a determinism-safe v4 UUID inside an app run.

Use this instead of ``uuid.uuid4()`` inside ``run()`` and runtime
interactions — the standard library version is non-deterministic and
breaks replay.

Example::

    from application_sdk.app import uuid4
    run_id = str(uuid4())
"""

InteractionUnfinishedPolicy = _HandlerUnfinishedPolicy
"""Policy applied to in-flight runtime interactions when an app run exits.

Controls what happens if the app run completes or is cancelled before
an in-flight ``@update`` or ``@signal`` interaction has been acknowledged.
Pass via the decorator::

    @update(unfinished_policy=InteractionUnfinishedPolicy.ABANDON)
    async def stop(self, input: StopInput) -> StopOutput:
        ...

Members:

* ``WARN_AND_ABANDON`` — log a warning and drop the interaction (default).
* ``ABANDON`` — silently drop without a warning.
"""

__all__ = [
    "App",
    "AppContext",
    "AppError",
    "AppRegistry",
    "AtlanClientMixin",
    "EntryPointMetadata",
    "Input",
    "InteractionUnfinishedPolicy",
    "NonRetryableError",
    "Output",
    "OutputStatus",
    "RetryPolicy",
    "RetryableError",
    "TaskMetadata",
    "TaskRegistry",
    "entrypoint",
    "mcp_tool",
    "now",
    "query",
    "signal",
    "sleep",
    "task",
    "update",
    "uuid4",
    "wait_condition",
]
