"""One-call scenario harness for testing Apps in-process.

``AppTestHarness`` composes the SDK's existing in-memory infrastructure
fakes (``MockStateStore``, ``MockSecretStore``, ``MockCredentialStore``,
``MockHeartbeatController`` and an obstore ``MemoryStore``/``LocalStore``)
into a ready-to-run execution environment for an :class:`~application_sdk.app.base.App`
subclass — no Dapr sidecar, no Temporal server, no Docker.

Per ADR-0005, app code only touches infrastructure through ``self.context``
and the Protocol-based storage helpers, so the exact same ``@task`` bodies
that run as Temporal activities in production execute inline here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.app.task import get_task_metadata, is_task
from application_sdk.contracts.base import Input, Output
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    get_infrastructure,
    set_infrastructure,
)
from application_sdk.storage.factory import create_local_store, create_memory_store
from application_sdk.testing.mocks import (
    MockCredentialStore,
    MockHeartbeatController,
    MockSecretStore,
    MockStateStore,
)

if TYPE_CHECKING:
    from pathlib import Path

    from obstore.store import ObjectStore

    from application_sdk.app.base import App


class AppTestHarness:
    """In-process execution environment for an App under test.

    Wires a fake :class:`~application_sdk.app.context.AppContext`, an
    in-memory state store, an in-memory secret/credential store, a local
    objectstore, and heartbeat capture around a fresh instance of your App —
    then lets you execute ``@task`` methods (or ``run()`` / entry points)
    exactly as production would, but inline.

    While the harness is entered, the SDK's module-level infrastructure
    context points at the same fakes, so storage helpers called without an
    explicit store (``await upload_file(key, path)``) resolve to the
    harness objectstore too.  The previous infrastructure context is
    restored on exit.

    Args:
        app_cls: The App subclass under test.
        secrets: Initial secrets, available via ``self.context.get_secret``.
        store_root: Optional directory for an on-disk ``LocalStore``.  When
            omitted, an in-memory store is used (fastest; no teardown).

    Captured evidence (inspect after executing):
        * ``harness.outputs`` — every ``Output`` returned by a ``@task``
        * ``harness.state_store`` — saved state (``get_save_calls()``)
        * ``harness.heartbeats`` — recorded heartbeats (``get_heartbeat_calls()``)
        * ``harness.store`` — the objectstore (pass to ``storage.list_keys`` etc.)

    Example::

        from application_sdk.storage import list_keys
        from application_sdk.testing import AppTestHarness

        class GreeterApp(App):
            @task
            async def greet(self, input: GreetInput) -> GreetOutput:
                await self.context.save_state("last", {"name": input.name})
                return GreetOutput(message=f"hello {input.name}")

            async def run(self, input: GreetInput) -> GreetOutput:
                return await self.greet(input)

        async def test_greeter_end_to_end():
            with AppTestHarness(GreeterApp, secrets={"token": "t"}) as harness:
                out = await harness.execute("run", GreetInput(name="ada"))

                assert out.message == "hello ada"
                assert harness.outputs[0].message == "hello ada"
                saves = harness.state_store.get_save_calls()
                assert saves[0][1] == {"name": "ada"}
                assert await list_keys("", harness.store) == []

    Note:
        Defining App subclasses registers them globally; use the
        ``clean_app_registry`` / ``clean_task_registry`` fixtures from
        :mod:`application_sdk.testing.fixtures` in your conftest when tests
        declare their own Apps.
    """

    def __init__(
        self,
        app_cls: type[App],
        *,
        secrets: dict[str, str] | None = None,
        store_root: str | Path | None = None,
    ) -> None:
        self._app_cls = app_cls
        self.credentials: MockCredentialStore = MockCredentialStore()
        # One secret store backs both context.get_secret() and
        # context.resolve_credential(): refs minted via
        # ``harness.credentials.add_*`` resolve without extra wiring.
        self.secret_store: MockSecretStore = self.credentials.secret_store
        for name, value in (secrets or {}).items():
            self.secret_store.set(name, value)
        self.state_store: MockStateStore = MockStateStore()
        self.heartbeats: MockHeartbeatController = MockHeartbeatController()
        self.store: ObjectStore = (
            create_local_store(store_root)
            if store_root is not None
            else create_memory_store()
        )
        self.outputs: list[Output] = []

        self.context: AppContext = AppContext(
            app_name=getattr(app_cls, "_app_name", app_cls.__name__),
            app_version=getattr(app_cls, "_app_version", "0.0.0"),
            _state_store=self.state_store,
            _secret_store=self.secret_store,
            _storage=self.store,
        )
        self._app: App | None = None
        self._previous_infrastructure: InfrastructureContext | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> AppTestHarness:
        self._previous_infrastructure = get_infrastructure()
        set_infrastructure(
            InfrastructureContext(
                state_store=self.state_store,
                secret_store=self.secret_store,
                storage=self.store,
            )
        )
        app = self._app_cls()
        app._context = self.context
        self._wrap_instance_tasks(app)
        self._app = app
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._app is not None:
            self._app._context = None
            self._app._task_context = None
            self._app = None
        set_infrastructure(
            self._previous_infrastructure
            if self._previous_infrastructure is not None
            else InfrastructureContext()
        )
        self._previous_infrastructure = None

    @property
    def app(self) -> App:
        """The live App instance (context wired, tasks executable inline)."""
        if self._app is None:
            raise RuntimeError(
                "AppTestHarness must be entered first: "
                "use `with AppTestHarness(MyApp) as harness:`"
            )
        return self._app

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, method: str | Any, input: Input) -> Output:
        """Execute a ``@task``, ``run()``, or entry-point method inline.

        Args:
            method: Method name (``"run"``, ``"greet"``) or the method
                object itself (``GreeterApp.greet``).
            input: The single ``Input`` contract instance for the method.

        Returns:
            The method's ``Output``.  Task outputs are also appended to
            :attr:`outputs` (including tasks invoked indirectly via
            ``run()``).
        """
        name = method if isinstance(method, str) else method.__name__
        bound = getattr(self.app, name)
        result: Output = await bound(input)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wrap_instance_tasks(self, app: App) -> None:
        """Mirror production task wrapping, executing inline instead of via Temporal.

        Production (``_wrap_instance_tasks`` in ``app.base``) replaces each
        ``@task`` method with a Temporal ``execute_activity`` dispatch.  The
        harness replaces them with an inline call that installs a
        ``TaskExecutionContext`` (heartbeats, ``run_in_thread``) for the
        duration of the task and records the returned ``Output``.
        """
        for attr_name in dir(type(app)):
            if attr_name.startswith("_"):
                continue
            attr = getattr(type(app), attr_name, None)
            if attr is None or not is_task(attr):
                continue
            meta = get_task_metadata(attr)
            task_name = meta.name if meta is not None else attr_name
            setattr(
                app,
                attr_name,
                self._make_inline_task_runner(app, attr_name, task_name),
            )

    def _make_inline_task_runner(
        self, app: App, attr_name: str, task_name: str
    ) -> Any:
        original = getattr(type(app), attr_name)

        async def runner(input_data: Input) -> Output:
            previous = app._task_context
            app._task_context = TaskExecutionContext(
                app_context=self.context,
                task_name=task_name,
                heartbeat_controller=self.heartbeats,
            )
            try:
                output: Output = await original(app, input_data)
                self.outputs.append(output)
                return output
            finally:
                app._task_context = previous

        return runner


__all__ = ["AppTestHarness"]
