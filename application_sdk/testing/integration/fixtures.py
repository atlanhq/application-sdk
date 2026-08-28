"""The canonical in-process integration fixture set, parameterized by override.

Every v3 connector's ``tests/integration/conftest.py`` boots the same thing: an
embedded Temporal dev server, mocked secret / state / storage infrastructure, an
in-process worker, and a thin executor shim over
:class:`~application_sdk.execution.TemporalExecutorBackend`. Only the App class
and the source are genuinely per-connector — the task queue is derived, by the
same call the worker and the served manifest make. This module ships the rest as
ordinary pytest fixtures; a connector star-imports them and overrides the
``integration_*`` fixtures it needs to::

    import os

    os.environ.setdefault("ATLAN_APPLICATION_NAME", "yourapp")
    os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

    import pytest  # noqa: E402

    from application_sdk.testing.integration.fixtures import *  # noqa: E402, F403

    from app.connector import YourApp  # noqa: E402


    @pytest.fixture(scope="session")
    def integration_app_cls() -> type[YourApp]:
        return YourApp


    @pytest.fixture(scope="session")
    def integration_source():
        ...  # a testcontainer, an HTTP fake, whatever this connector extracts from

Everything else is a plain pytest override. A suite that seeds credentials
overrides ``integration_secrets``; one that needs a real store overrides
``infrastructure`` itself; one that wants Prometheus on overrides
``integration_options``. There is no binding boilerplate and nothing to name
correctly — the kit's fixtures request one another by their own names, and a
suite that wants a different test-facing name aliases in the usual way::

    @pytest.fixture(scope="session")
    def yourapp_executor(executor):
        return executor

**Why this is a star-import and not a ``pytest11`` plugin.** A plugin is loaded
before any conftest runs, so a module-level ``application_sdk`` import inside it
would snapshot ``APPLICATION_NAME`` / ``DEPLOYMENT_NAME`` into
:mod:`application_sdk.constants` *before* the conftest's ``os.environ.setdefault``
lines execute. Importing from the conftest — below those lines — is the only
placement that keeps the env-before-import rule satisfiable, so that constraint
decides the shape.

Three ordering rules the reference conftests carry as comments are structural
here instead:

* **Infrastructure before the worker** — ``worker`` depends on
  ``infrastructure``, so there is no parameter for an adopter to drop.
* **App registration before ``create_worker``** — ``create_worker`` snapshots
  the registries at call time, and a worker built before the App import
  registers zero workflows, starts anyway, then fails every workflow task for
  an unregistered type. ``worker`` verifies the App is in the registry before
  building anything.
* **SDK env vars before the first ``application_sdk`` import** — this one
  cannot be delegated to an SDK import, because importing anything under
  ``application_sdk.testing`` is itself the import that snapshots the
  constants. The ``os.environ.setdefault`` lines therefore stay in the
  conftest, above the imports — and this module compares the snapshot against
  the live environment *when it is imported*, raising
  :class:`~application_sdk.testing.integration._errors.IntegrationEnvOrderingError`
  when they disagree.

That import-time check is deliberately loud: a violation surfaces as a
collection error for the whole session rather than a per-test failure, because
a mis-ordered conftest mistags every test's observability output and there is
no meaningful subset to keep running. The registration check in ``worker``
fails the session the same way, at first fixture setup.

Adopting these fixtures also means adopting their loop scope: every async
fixture here is pinned ``loop_scope="session"``, so the suite's own tests must
run on the session loop too, or pytest-asyncio fails or mis-schedules them. Set
both ``asyncio_default_fixture_loop_scope`` and
``asyncio_default_test_loop_scope`` to ``"session"`` in ``pyproject.toml``'s
``[tool.pytest.ini_options]``, or mark per-test
``@pytest.mark.asyncio(loop_scope="session")``. See conformance rule T019.

``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR`` is read at run time, not at
import, so the kit can own it. It defaults to ``true`` in the SDK, and with it
on ``App.on_complete()`` deletes the run's local files and every tracked
``TRANSIENT`` object-store ref after each run — so a suite that opens output
files and asserts on their contents needs it off.
``KitOptions.preserve_artifacts`` (on by default) **defaults** the variable to
``"false"`` when it is unset; an explicit value in the environment wins, and a
truthy one is logged as a warning so a CI job that exports it does not fail
every artifact assertion with "output file missing" and nothing pointing at the
cause. The option is one-way: ``preserve_artifacts=False`` leaves the variable
untouched rather than forcing ``"true"``.

Mocked infrastructure is the default for this tier: ``MockSecretStore``,
``MockStateStore`` and a ``LocalStore`` under a session temp dir. A suite that
needs something else overrides the ``infrastructure`` fixture; it receives
``store_root`` and ``integration_source`` like any other fixture, so a real
store can be pointed at a container the source fixture brought up. An async
lifecycle — a ``daprd`` sidecar, anything needing ``await`` — is out of scope
for these fixtures; ``atlan-mysql-app`` is the standing exception and the
reference for that hand-written shape.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.integration._errors import (
    AppRegistrationMissingError,
    IntegrationEnvOrderingError,
)

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.app.base import App
    from application_sdk.dev import EmbeddedRuntime
    from application_sdk.execution import TemporalExecutorBackend
    from application_sdk.infrastructure.context import InfrastructureContext

logger = get_logger(__name__)

CLEANUP_INTERCEPTOR_ENV = "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"
"""Env var gating ``App.on_complete()``'s file and object-store cleanup."""

APPLICATION_NAME_ENV = "ATLAN_APPLICATION_NAME"
DEPLOYMENT_NAME_ENV = "ATLAN_DEPLOYMENT_NAME"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_SETDEFAULT_FIX = (
    "Move the os.environ.setdefault(...) call above every application_sdk "
    "import in conftest.py — application_sdk.constants snapshots these into "
    "module-level constants on first import, and eleven modules re-bind those "
    "constants into their own namespace, so an assignment made afterwards "
    "never reaches them."
)


@dataclass(frozen=True)
class KitOptions:
    """Knobs for the fixture set. Override ``integration_options`` to change one.

    Attributes:
        data_converter: Pass ``create_data_converter_for_app(app_cls)`` to the
            client. On by default: the converter is what round-trips the App's
            typed inputs and outputs across the workflow boundary, so its
            absence is a latent serialization bug rather than a preference.
        enable_prometheus: Off by default. The Temporal Rust-core runtime binds
            a *fixed* Prometheus port once per process, and integration jobs run
            under ``pytest -n auto --dist=loadfile`` — one process per test
            file, all racing for that port. A test client needs no metrics.
        log_level: Embedded dev-server log level.
        preserve_artifacts: Default ``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR``
            to ``"false"`` when unset, so the run's output files survive for the
            suite to assert on. An explicit environment value always wins.
        store_root_prefix: ``tmp_path_factory`` prefix for the LocalStore root.
    """

    data_converter: bool = True
    enable_prometheus: bool = False
    log_level: str = "error"
    preserve_artifacts: bool = True
    store_root_prefix: str = "sdk-store"


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

    backend: TemporalExecutorBackend

    async def execute_app(
        self,
        app_cls: type[App],
        input_data: object,
        *,
        execution_id_prefix: str = "",
        entry_point: str | None = None,
    ) -> object:
        from application_sdk.app.context import AppContext  # noqa: PLC0415
        from application_sdk.execution.retry import RetryPolicy  # noqa: PLC0415

        app_name = _app_name_of(app_cls) or execution_id_prefix or "app"
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


def _app_name_of(app_cls: type[App]) -> str | None:
    """The App's registered name, or ``None`` when it has none.

    ``_app_name`` is stamped onto the class by registration
    (:func:`application_sdk.app._ep_registration`), not by ``App`` itself — so
    its absence is not a missing attribute to route around, it *is* the
    unregistered case :func:`_verify_registration` reports. One reader here
    keeps the three call sites from each inventing a different fallback.
    """
    name = getattr(app_cls, "_app_name", None)
    return name if isinstance(name, str) and name else None


def _verify_env_ordering() -> None:
    """Fail when an ``ATLAN_*`` env var was set after the constants snapshot."""
    from application_sdk import constants  # noqa: PLC0415

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


def _verify_registration(app_cls: type[App]) -> str:
    """Fail when *app_cls* never reached the App registry; else its name."""
    from application_sdk.app.registry import AppRegistry  # noqa: PLC0415

    app_name = _app_name_of(app_cls)
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
                "Import the App class in conftest.py, above the fixture "
                "overrides; the import is what populates the registry."
            ),
        )
    return app_name


def _apply_artifact_preservation(options: KitOptions) -> None:
    """Default the cleanup interceptor off; warn when the environment overrides."""
    if not options.preserve_artifacts:
        return
    current = os.environ.get(CLEANUP_INTERCEPTOR_ENV)
    if current is None:
        os.environ[CLEANUP_INTERCEPTOR_ENV] = "false"
        return
    if current.strip().lower() in _TRUTHY:
        logger.warning(
            "%s=%r is set in the environment, so App.on_complete() will delete "
            "each run's output files and this suite's artifact assertions will "
            "fail with 'output file missing'. Unset it, or set it to 'false', "
            "to preserve artifacts as KitOptions.preserve_artifacts intends.",
            CLEANUP_INTERCEPTOR_ENV,
            current,
        )


_verify_env_ordering()


@pytest.fixture(scope="session")
def integration_app_cls() -> type[App]:
    """The App class under test. Every adopting conftest overrides this."""
    raise AppRegistrationMissingError(
        message=(
            "No App class configured for the integration fixtures — the "
            "conftest must override the integration_app_cls fixture."
        ),
        resource="integration_app_cls",
        expected_state="overridden in tests/integration/conftest.py",
        actual_state="default",
        suggested_action=(
            "Add a session-scoped integration_app_cls fixture returning the "
            "App class, below the star-import of "
            "application_sdk.testing.integration.fixtures."
        ),
    )


@pytest.fixture(scope="session")
def integration_task_queue(integration_app_cls: type[App]) -> str:
    """Task queue the worker listens on and the executor submits to.

    Derived by :func:`application_sdk.common.task_queue.task_queue_from_env` —
    the *same* call :func:`application_sdk.main._derive_task_queue` makes, and
    the same value the served manifest stamps for the Automation Engine. That
    module exists because the worker and the manifest once derived this name
    independently and disagreed, and nothing failed loudly: AE submitted to one
    queue, the worker polled another, and the run sat unclaimed until its 24h
    heartbeat backstop (CONNECT-183, FND-195).

    A local re-derivation here would rebuild exactly that. With the conftest's
    ``ATLAN_APPLICATION_NAME=yourapp`` / ``ATLAN_DEPLOYMENT_NAME=ci`` the real
    queue is ``atlan-yourapp-ci``; a ``"<app>-queue"`` literal would be
    self-consistent across this suite's own worker and executor and therefore
    green, while testing a queue name no deployment ever uses.

    Only the no-app-name fallback is local, matching ``_derive_task_queue``:
    ``{app}-queue`` predates the env-var convention and is load-bearing for
    local dev, where nothing reads the manifest's queue anyway.
    """
    from application_sdk.common.task_queue import task_queue_from_env  # noqa: PLC0415

    return task_queue_from_env() or f"{_verify_registration(integration_app_cls)}-queue"


@pytest.fixture(scope="session")
def integration_source() -> object:
    """Whatever this connector extracts from; ``None`` until overridden.

    A testcontainer, an in-process HTTP fake and a plain credential dict are all
    equally acceptable. Nothing here assumes anything about its type; it is
    handed to ``integration_secrets`` and otherwise untouched.
    """
    return None


@pytest.fixture(scope="session")
def integration_secrets(integration_source: object) -> Mapping[str, str]:
    """Seed for the mocked secret store, as a ``{key: json}`` mapping.

    Empty by default, which suits suites that pass credentials inline in the
    workflow input. Override to seed from the resolved source.

    Serves ``credential_ref`` named-path and agent-spec resolution only. An
    input routed by legacy ``credential_guid`` resolves through
    ``DaprCredentialVault`` over a live daprd and never reads this store; suites
    for those apps pass credentials inline, seed a GUID via the app's
    ``/workflows/v1/dev/local-vault`` dev endpoint, or stay off these fixtures.
    """
    return {}


@pytest.fixture(scope="session")
def integration_options() -> KitOptions:
    """The kit's knobs. Override to return a customised :class:`KitOptions`."""
    return KitOptions()


@pytest.fixture(scope="session")
def store_root(
    tmp_path_factory: pytest.TempPathFactory, integration_options: KitOptions
) -> Path:
    """Root of the session-scoped LocalStore backing the object store."""
    return tmp_path_factory.mktemp(integration_options.store_root_prefix)


@pytest.fixture(scope="session")
def infrastructure(
    store_root: Path,
    integration_source: object,
    integration_secrets: Mapping[str, str],
) -> Iterator[InfrastructureContext]:
    """Wire mocked infrastructure for the session, after the source is up.

    Override this fixture to install anything else; it is torn down the same
    way. The observability deployment store is pointed at an in-memory store
    for the session so the periodic flush stops retrying against a store that
    is not there, and restored afterwards.
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415
        InfrastructureContext,
        clear_infrastructure,
        set_infrastructure,
    )
    from application_sdk.observability.observability import (  # noqa: PLC0415
        AtlanObservability,
    )
    from application_sdk.storage import (  # noqa: PLC0415
        create_local_store,
        create_memory_store,
    )
    from application_sdk.testing.mocks import (  # noqa: PLC0415
        MockSecretStore,
        MockStateStore,
    )

    del integration_source
    ctx = InfrastructureContext(
        state_store=MockStateStore(),
        secret_store=MockSecretStore(dict(integration_secrets)),
        storage=create_local_store(store_root),
    )
    previous_store = AtlanObservability._deployment_store
    AtlanObservability._deployment_store = create_memory_store()
    set_infrastructure(ctx)
    try:
        yield ctx
    finally:
        clear_infrastructure()
        AtlanObservability._deployment_store = previous_store


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def embedded_temporal(
    integration_options: KitOptions,
) -> AsyncIterator[EmbeddedRuntime]:
    """Boot the embedded Temporal dev server for the session."""
    from application_sdk.dev import embedded_runtime  # noqa: PLC0415

    async with embedded_runtime(log_level=integration_options.log_level) as runtime:
        yield runtime


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_client(
    embedded_temporal: EmbeddedRuntime,
    integration_app_cls: type[App],
    integration_options: KitOptions,
) -> Client:
    """Connect to the embedded dev server, in its namespace."""
    from application_sdk.execution import (  # noqa: PLC0415
        create_data_converter_for_app,
        create_temporal_client,
    )

    converter = (
        create_data_converter_for_app(integration_app_cls)
        if integration_options.data_converter
        else None
    )
    return await create_temporal_client(
        host=embedded_temporal.host,
        namespace=embedded_temporal.namespace,
        data_converter=converter,
        enable_prometheus=integration_options.enable_prometheus,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def worker(
    temporal_client: Client,
    infrastructure: InfrastructureContext,
    integration_app_cls: type[App],
    integration_task_queue: str,
    integration_options: KitOptions,
) -> AsyncIterator[None]:
    """Run the App's worker in-process, with infrastructure already wired."""
    from application_sdk.execution import create_worker  # noqa: PLC0415

    del infrastructure
    _verify_registration(integration_app_cls)
    _apply_artifact_preservation(integration_options)
    async with create_worker(temporal_client, task_queue=integration_task_queue):
        yield


@pytest.fixture(scope="session")
def executor(
    temporal_client: Client, worker: None, integration_task_queue: str
) -> AppExecutor:
    """Executor submitting to the running worker's task queue."""
    from application_sdk.execution import TemporalExecutorBackend  # noqa: PLC0415

    del worker
    return AppExecutor(
        backend=TemporalExecutorBackend(
            client=temporal_client, task_queue=integration_task_queue
        )
    )


__all__ = [
    "APPLICATION_NAME_ENV",
    "CLEANUP_INTERCEPTOR_ENV",
    "DEPLOYMENT_NAME_ENV",
    "AppExecutor",
    "KitOptions",
    "embedded_temporal",
    "executor",
    "infrastructure",
    "integration_app_cls",
    "integration_options",
    "integration_secrets",
    "integration_source",
    "integration_task_queue",
    "store_root",
    "temporal_client",
    "worker",
]
