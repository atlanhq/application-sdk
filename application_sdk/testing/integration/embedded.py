"""The canonical in-process integration fixture set, parameterized on the source.

Every v3 connector's ``tests/integration/conftest.py`` boots the same thing: an
embedded Temporal dev server, mocked secret / state / storage infrastructure, an
in-process worker, and a thin executor shim over
:class:`~application_sdk.execution.TemporalExecutorBackend`. Only three parts are
genuinely per-connector — the App class, the task queue, and the fixture that
brings up the source. :func:`integration_kit` supplies everything else.

A connector's conftest becomes::

    import os

    os.environ.setdefault("ATLAN_APPLICATION_NAME", "yourapp")
    os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

    from application_sdk.testing.integration.embedded import integration_kit  # noqa: E402

    from app.connector import YourApp  # noqa: E402, F401 — triggers App registration

    _kit = integration_kit(
        app_cls=YourApp,
        task_queue="yourapp-queue",
        source_fixture="yourapp_source",
    )
    store_root = _kit.store_root
    infrastructure = _kit.infrastructure
    embedded_temporal = _kit.embedded_temporal
    temporal_client = _kit.temporal_client
    worker = _kit.worker
    executor = _kit.executor

    @pytest.fixture(scope="session")
    def yourapp_source():
        ...  # a testcontainer, an HTTP fake, whatever this connector extracts from

**Bind all six under exactly those names.** The kit's fixtures depend on each
other by name — that is what makes the ordering rules below structural — so
renaming one at the binding site breaks resolution. A suite whose tests already
use a different name should alias instead, which costs three lines and keeps the
real graph intact::

    @pytest.fixture(scope="session")
    def yourapp_executor(executor):
        return executor

The source fixture is named, never imported: ``source_fixture`` is resolved with
``request.getfixturevalue(...)``, so a testcontainer, an in-process HTTP fake and
a plain credential dict are all equally acceptable, and this module needs no
knowledge of any of them.

Three ordering rules the reference conftests carry as comments are structural
here instead:

* **Infrastructure before the worker** — the ``worker`` fixture depends on
  ``infrastructure`` inside the kit, so there is no parameter for an adopter to
  drop.
* **App registration before ``create_worker``** — ``create_worker`` snapshots the
  registries at call time, and a worker built before the App import registers
  zero workflows, starts anyway, then fails every workflow task for an
  unregistered type. Passing ``app_cls`` makes the registering import a
  precondition of calling the factory, and the factory verifies the App actually
  landed in the registry.
* **SDK env vars before the first ``application_sdk`` import** — this one cannot
  be delegated to an SDK import, because importing anything under
  ``application_sdk.testing`` is itself the import that snapshots
  ``APPLICATION_NAME`` / ``DEPLOYMENT_NAME`` into
  :mod:`application_sdk.constants`. The ``os.environ.setdefault`` lines therefore
  stay in the conftest, above the imports — but the factory compares the snapshot
  against the live environment and raises
  :class:`~application_sdk.testing.integration._errors.IntegrationEnvOrderingError`
  when they disagree, so a too-late assignment fails loudly instead of silently
  mistagging the suite's observability output.

``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR`` is different: it is read at run
time, not at import, so the kit owns it outright. It defaults to ``true``, and
with it on ``App.on_complete()`` deletes the run's local files and every tracked
``TRANSIENT`` object-store ref plus its ``.sha256`` sidecar after each run — so a
suite that opens output files and asserts on their contents needs it off.
``preserve_artifacts=True`` (the default here) sets it to ``"false"``.

Mocked infrastructure is the default for this tier. Real infrastructure — a
``daprd`` sidecar and the production credential-vault path — is available by
passing ``infrastructure_factory``, and stays an explicit, per-suite decision.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

CLEANUP_INTERCEPTOR_ENV = "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"
"""Env var gating ``App.on_complete()``'s file and object-store cleanup."""

APPLICATION_NAME_ENV = "ATLAN_APPLICATION_NAME"
DEPLOYMENT_NAME_ENV = "ATLAN_DEPLOYMENT_NAME"

_SETDEFAULT_FIX = (
    "Move the os.environ.setdefault(...) call above every application_sdk "
    "import in conftest.py — application_sdk.constants snapshots these into "
    "module-level constants on first import, and eleven modules re-bind those "
    "constants into their own namespace, so an assignment made afterwards "
    "never reaches them."
)


@dataclass(frozen=True)
class AppExecutor:
    """Thin shim over :class:`TemporalExecutorBackend` for integration suites.

    ``entry_point`` is optional and must be passed by an App that declares only
    explicit ``@entrypoint`` methods; such an App registers no bare ``{app}``
    workflow type. Omitting it there is safe rather than silent — the backend
    resolves the workflow type before submitting and raises
    ``EntryPointRequiredError`` / ``UnknownEntryPointError`` — but the parameter
    has to exist for a multi-entry-point app to submit at all.
    """

    backend: Any

    async def execute_app(
        self,
        app_cls: Any,
        input_data: Any,
        *,
        execution_id_prefix: str = "",
        entry_point: str | None = None,
    ) -> Any:
        from application_sdk.app.context import AppContext  # noqa: PLC0415
        from application_sdk.execution.retry import RetryPolicy  # noqa: PLC0415

        app_name = getattr(app_cls, "_app_name", execution_id_prefix or "app")
        context = AppContext(
            app_name=app_name,
            app_version="0.0.0",
            run_id=execution_id_prefix or app_name,
        )
        return await self.backend.execute(
            app_cls,
            input_data,
            context=context,
            retry_policy=RetryPolicy(),
            entry_point=entry_point,
        )


@dataclass(frozen=True)
class IntegrationKit:
    """The fixtures :func:`integration_kit` built, for binding into a conftest.

    Bind them by assignment (``store_root = _kit.store_root``) rather than by
    updating ``globals()``, so the suite's fixture graph stays greppable in the
    repo that owns it. Bind each under its attribute name here: these fixtures
    request one another by name, so a renamed binding does not resolve. Alias
    instead when a suite needs a different test-facing name.
    """

    store_root: Any
    infrastructure: Any
    embedded_temporal: Any
    temporal_client: Any
    worker: Any
    executor: Any


def _verify_env_ordering() -> None:
    """Fail when an ``ATLAN_*`` env var was set after the constants snapshot."""
    from application_sdk import constants  # noqa: PLC0415
    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        IntegrationEnvOrderingError,
    )

    for env_var, snapshot in (
        (APPLICATION_NAME_ENV, constants.APPLICATION_NAME),
        (DEPLOYMENT_NAME_ENV, constants.DEPLOYMENT_NAME),
    ):
        live = os.environ.get(env_var)
        if live is None:
            logger.warning(
                "%s is unset, so this suite's observability output is attributed "
                "to %r. Set it with os.environ.setdefault above the "
                "application_sdk imports in conftest.py.",
                env_var,
                snapshot,
            )
            continue
        if live != snapshot:
            raise IntegrationEnvOrderingError(
                message=(
                    f"{env_var} is {live!r} in the environment but "
                    f"application_sdk.constants snapshotted {snapshot!r}, so it "
                    "was set after the first application_sdk import."
                ),
                resource=env_var,
                expected_state=live,
                actual_state=snapshot,
                suggested_action=_SETDEFAULT_FIX,
            )


def _verify_registration(app_cls: type) -> None:
    """Fail when *app_cls* never reached the App registry."""
    from application_sdk.app.registry import AppRegistry  # noqa: PLC0415
    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        AppRegistrationMissingError,
    )

    app_name = getattr(app_cls, "_app_name", None)
    registered = AppRegistry.get_instance().list_apps()
    if app_name is None or app_name not in registered:
        raise AppRegistrationMissingError(
            message=(
                f"{app_cls.__name__} is not in the App registry, so create_worker "
                "would snapshot zero workflows for it — the worker would start "
                "successfully and then fail every workflow task it is handed, "
                "for an unregistered workflow type."
            ),
            resource=app_cls.__name__,
            expected_state="registered",
            actual_state=f"registry holds {sorted(registered)}",
            suggested_action=(
                "Import the App class in conftest.py before calling "
                "integration_kit(); the import is what populates the registry."
            ),
        )


def integration_kit(
    *,
    app_cls: type,
    task_queue: str,
    source_fixture: str | None = None,
    secrets: Callable[[Any], Mapping[str, str]] | None = None,
    infrastructure_factory: Callable[[Path], Any] | None = None,
    data_converter: bool = True,
    enable_prometheus: bool = False,
    log_level: str = "error",
    preserve_artifacts: bool = True,
    store_root_prefix: str = "sdk-store",
) -> IntegrationKit:
    """Build the canonical integration fixture set for *app_cls*.

    Args:
        app_cls: The App class under test. Must already be imported and
            registered — the factory verifies it.
        task_queue: Task queue the worker listens on and the executor submits to.
            Convention is ``"<app>-queue"``.
        source_fixture: Name of the connector's own source fixture. Resolved
            through ``request.getfixturevalue(...)`` before infrastructure is
            wired, so bringing the source up gates the rest of the session. Any
            fixture works; nothing about its type is assumed.
        secrets: Optional seed for the mocked secret store, called with the
            resolved source-fixture value and returning a ``{key: json}`` mapping.
            Omit for suites that pass credentials inline in the workflow input.
        infrastructure_factory: Opt-in escape hatch, called with the session's
            store root and returning an ``InfrastructureContext`` this kit will
            install as-is. Use it for a suite that must exercise real
            infrastructure (a ``daprd`` sidecar, the production credential-vault
            path). Mocked infrastructure remains the default.
        data_converter: Pass ``create_data_converter_for_app(app_cls)`` to the
            client. On by default: the converter is what round-trips the App's
            typed inputs and outputs across the workflow boundary, so its
            absence is a latent serialization bug rather than a preference.
        enable_prometheus: Off by default. The Temporal Rust-core runtime binds
            a *fixed* Prometheus port once per process, and integration jobs run
            under ``pytest -n auto --dist=loadfile`` — one process per test file,
            all racing for that port. A test client needs no metrics endpoint.
        log_level: Embedded dev-server log level.
        preserve_artifacts: Disable the cleanup interceptor so the run's output
            files survive for the suite to assert on. On by default.
        store_root_prefix: ``tmp_path_factory`` prefix for the LocalStore root.

    Returns:
        An :class:`IntegrationKit` whose attributes are the six fixtures.
    """
    _verify_env_ordering()
    _verify_registration(app_cls)

    if preserve_artifacts:
        os.environ.setdefault(CLEANUP_INTERCEPTOR_ENV, "false")

    import pytest_asyncio  # noqa: PLC0415 — only the kit needs the async plugin

    from application_sdk.observability.observability import (  # noqa: PLC0415
        AtlanObservability,
    )
    from application_sdk.storage import create_memory_store  # noqa: PLC0415

    AtlanObservability._deployment_store = create_memory_store()

    @pytest.fixture(scope="session")
    def store_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Root of the session-scoped LocalStore backing the object store."""
        return tmp_path_factory.mktemp(store_root_prefix)

    @pytest.fixture(scope="session")
    def infrastructure(request: pytest.FixtureRequest, store_root: Path) -> Any:
        """Wire the session's infrastructure, after the source is up."""
        from application_sdk.infrastructure.context import (  # noqa: PLC0415
            InfrastructureContext,
            set_infrastructure,
        )
        from application_sdk.storage import create_local_store  # noqa: PLC0415
        from application_sdk.testing.mocks import (  # noqa: PLC0415
            MockSecretStore,
            MockStateStore,
        )

        source = (
            request.getfixturevalue(source_fixture)
            if source_fixture is not None
            else None
        )
        if infrastructure_factory is not None:
            ctx = infrastructure_factory(store_root)
        else:
            seeded = dict(secrets(source)) if secrets is not None else {}
            ctx = InfrastructureContext(
                state_store=MockStateStore(),
                secret_store=MockSecretStore(seeded),
                storage=create_local_store(store_root),
            )
        set_infrastructure(ctx)
        return ctx

    @pytest_asyncio.fixture(scope="session")
    async def embedded_temporal() -> Any:
        """Boot the embedded Temporal dev server for the session."""
        from application_sdk.dev import embedded_runtime  # noqa: PLC0415

        async with embedded_runtime(log_level=log_level) as runtime:
            yield runtime

    @pytest_asyncio.fixture(scope="session")
    async def temporal_client(embedded_temporal: Any) -> Any:
        """Connect to the embedded dev server."""
        from application_sdk.execution import (  # noqa: PLC0415
            create_data_converter_for_app,
            create_temporal_client,
        )

        converter = create_data_converter_for_app(app_cls) if data_converter else None
        return await create_temporal_client(
            host=embedded_temporal.host,
            data_converter=converter,
            enable_prometheus=enable_prometheus,
        )

    @pytest_asyncio.fixture(scope="session")
    async def worker(temporal_client: Any, infrastructure: Any) -> Any:  # noqa: ARG001
        """Run the App's worker in-process, with infrastructure already wired."""
        from application_sdk.execution import create_worker  # noqa: PLC0415

        async with create_worker(temporal_client, task_queue=task_queue):
            yield

    @pytest.fixture(scope="session")
    def executor(temporal_client: Any, worker: Any) -> AppExecutor:  # noqa: ARG001
        """Executor submitting to the running worker's task queue."""
        from application_sdk.execution import TemporalExecutorBackend  # noqa: PLC0415

        return AppExecutor(
            backend=TemporalExecutorBackend(
                client=temporal_client, task_queue=task_queue
            )
        )

    return IntegrationKit(
        store_root=store_root,
        infrastructure=infrastructure,
        embedded_temporal=embedded_temporal,
        temporal_client=temporal_client,
        worker=worker,
        executor=executor,
    )


__all__ = [
    "APPLICATION_NAME_ENV",
    "AppExecutor",
    "CLEANUP_INTERCEPTOR_ENV",
    "DEPLOYMENT_NAME_ENV",
    "IntegrationKit",
    "integration_kit",
]
