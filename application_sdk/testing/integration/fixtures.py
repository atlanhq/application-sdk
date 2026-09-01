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

A source with no image to pull uses :func:`http_fake_source_factory`, which ships
here rather than in :mod:`application_sdk.testing.fixtures` precisely so that the
factory and its autouse ``reset_http_fake_sources`` arrive together with the rest
of the kit — a suite cannot pick up one and miss the other. See
``docs/guides/integration-fixtures.md``.

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
``"false"`` when it is unset or empty; an explicit value in the environment wins,
and one that leaves cleanup *enabled* is logged as a warning so a CI job that
exports it does not fail every artifact assertion with "output file missing" and
nothing pointing at the cause. "Enabled" is decided by the same denylist the SDK
itself reads (:func:`_cleanup_enabled`) rather than by a second, re-derived
predicate — an allowlist here diverged for ``"off"``, ``"disabled"``,
``"  false  "`` and ``""``, staying silent in exactly the cases that delete the
artifacts. The default is scoped to the ``worker`` fixture's lifetime and the
prior value restored on teardown: ``pytest tests/`` runs integration and unit
tests in one process, and a cleanup-asserting unit test scheduled after this
fixture would otherwise silently observe cleanup disabled (BLDX-1283). The option
is one-way: ``preserve_artifacts=False`` leaves the variable untouched rather
than forcing ``"true"``.

``store_root`` covers the object store a run writes through. The local scratch
tree it also writes — the ``{TEMPORARY_PATH}/artifacts/apps/{APPLICATION_NAME}``
root the SDK derives a run's paths under — is left at the SDK default unless a
suite requests ``temporary_path``, which redirects it at a session temp dir. Two
pre-run guards cover the ways that identity goes wrong silently:
``_verify_app_name`` (an explicitly set ``ATLAN_APPLICATION_NAME`` that is not
the App's registered name splits the run's identity in two) and
``_verify_infrastructure`` (a per-test fixture that calls ``set_infrastructure``
after the session fixture sends the run to a store the fixtures never expose).
Both fail loudly, for the same reason the ordering and registration checks do.

Mocked infrastructure is the default for this tier: ``MockSecretStore``,
``MockStateStore`` and a ``LocalStore`` under a session temp dir. A suite that
needs something else overrides the ``infrastructure`` fixture — which
**replaces** it, since a star-imported fixture cannot be wrapped; call
:func:`kit_infrastructure` from the override to reuse this body instead of
copying it. The override receives ``store_root`` and ``integration_source`` like
any other fixture, so a real store can be pointed at a container the source
fixture brought up. An async
lifecycle — a ``daprd`` sidecar, anything needing ``await`` — is out of scope
for these fixtures; ``atlan-mysql-app`` is the standing exception and the
reference for that hand-written shape.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import pytest_asyncio

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.fake_source import HttpFakeSourceFactory
from application_sdk.testing.integration._errors import (
    AppNameMismatchError,
    AppRegistrationMissingError,
    InfrastructureReplacedError,
    IntegrationEnvOrderingError,
)

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.app.base import App
    from application_sdk.dev import EmbeddedRuntime
    from application_sdk.execution import TemporalExecutorBackend
    from application_sdk.infrastructure.context import InfrastructureContext

logger = get_logger(__name__)

_ACTIVE_FAKE_SOURCE_FACTORY: HttpFakeSourceFactory | None = None
"""The live ``http_fake_source_factory``, or ``None`` before one is requested.

Module-level rather than a fixture dependency because ``reset_http_fake_sources``
must not *instantiate* the session factory — an autouse fixture that requested it
would stand up a server for every suite that star-imports the kit, including the
ones whose source is a container.
"""

CLEANUP_INTERCEPTOR_ENV = "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"
"""Env var gating ``App.on_complete()``'s file and object-store cleanup."""

APPLICATION_NAME_ENV = "ATLAN_APPLICATION_NAME"
DEPLOYMENT_NAME_ENV = "ATLAN_DEPLOYMENT_NAME"

#: The values that *disable* cleanup, mirroring the SDK's own reader rather than
#: re-deriving one. ``App.on_complete`` gates on
#: ``os.environ.get(ENV, "true").lower() not in ("0", "false", "no")`` and
#: ``execution.settings._bool(..., default=True)`` agrees, so the flag is a
#: denylist defaulting to on: every value outside this set enables cleanup.
#:
#: An allowlist here instead (``{"1", "true", "yes", "on"}``) diverged for
#: ``"off"``, ``"disabled"``, ``"  false  "`` and ``""`` — the SDK deleted the
#: run's artifacts while this module stayed silent, which is exactly the
#: "output file missing" failure :attr:`KitOptions.preserve_artifacts` exists to
#: prevent. Note the SDK does *not* strip, so ``"  false  "`` enables cleanup.
_CLEANUP_DISABLED = frozenset({"0", "false", "no"})


def _cleanup_enabled(value: str) -> bool:
    """Whether *value* leaves ``App.on_complete()``'s cleanup switched on."""
    return value.lower() not in _CLEANUP_DISABLED


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
        temporary_path_prefix: ``tmp_path_factory`` prefix for the local run
            scratch root handed out by :func:`temporary_path`.
    """

    data_converter: bool = True
    enable_prometheus: bool = False
    log_level: str = "error"
    preserve_artifacts: bool = True
    store_root_prefix: str = "sdk-store"
    temporary_path_prefix: str = "sdk-local"


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
    expected_infrastructure: InfrastructureContext | None = None
    """The context :func:`infrastructure` installed, re-checked before each run.

    ``None`` disables the check, for an executor built outside the kit.
    """

    async def execute_app(
        self,
        app_cls: type[App],
        input_data: object,
        *,
        execution_id_prefix: str = "",
        entry_point: str | None = None,
    ) -> Any:
        # ``Any``, matching ``TemporalExecutorBackend.execute``, because the
        # concrete type is the App entrypoint's own Output and is not derivable
        # from ``type[App]`` here. ``object`` would be narrower than the truth
        # and make the documented ``output.<field>`` a type error at every
        # adopter's call site (pyright's default mode reports it; this repo
        # downgrades that rule and excludes ``tests``, so CI here would not).
        from application_sdk.app.context import (  # noqa: PLC0415 — deferred: keeps app.context off the conftest's import path
            AppContext,
        )
        from application_sdk.execution.retry import (  # noqa: PLC0415 — deferred: keeps execution (and temporalio) off the conftest's import path
            RetryPolicy,
        )

        _verify_infrastructure(self.expected_infrastructure)

        app_name = _app_name_of(app_cls) or execution_id_prefix or "app"
        context = AppContext(
            app_name=app_name,
            app_version="0.0.0",
            # Keep AppContext's own uuid4 default rather than reusing the app
            # name: ``__post_init__`` derives ``correlation_id`` from ``run_id``
            # and the backend stamps it into the Temporal start memo, so a
            # constant here logs every run in the suite under one identity — in
            # the tier whose purpose is debugging a single failing run.
            run_id=execution_id_prefix or str(uuid4()),
            execution_id_prefix=execution_id_prefix,
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
    from application_sdk import (  # noqa: PLC0415 — deferred by convention only; logger_adaptor above already imported it, so this is a re-bind, NOT what makes the ordering check work
        constants,
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


def _verify_registration(app_cls: type[App]) -> str:
    """Fail when *app_cls* never reached the App registry; else its name."""
    from application_sdk.app.registry import (  # noqa: PLC0415 — deferred: keeps app.registry off the conftest's import path
        AppRegistry,
    )

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
    _verify_app_name(app_cls, app_name)
    return app_name


def _verify_app_name(app_cls: type[App], app_name: str) -> None:
    """Fail when an explicitly set ``ATLAN_APPLICATION_NAME`` is not the App's name.

    The variable is what ``application_sdk.constants.APPLICATION_NAME`` carries,
    and the SDK builds a run's local artifact root from it
    (``{TEMPORARY_PATH}/artifacts/apps/{APPLICATION_NAME}/workflows/...``) while
    the App's own registered name drives the task queue and the observability
    tags. Disagreeing values do not fail anything loudly — the run writes its
    files under one identity and reports itself under another, and the suite
    looks for artifacts where they were never written.

    Only a suite that explicitly set the variable is checked — an adopter who
    never sets it gets the SDK default (``"default"``), which is a different
    conversation and not one to fail a session over. The value *compared* is
    ``constants.APPLICATION_NAME``, the import-time snapshot, because that is
    what actually builds the artifact root — the live environment is only the
    opt-in signal, so the guard stays correct even if something changes the
    variable mid-session (``_verify_env_ordering`` pins the two equal at
    import, but by construction beats by side effect).
    """
    if os.environ.get(APPLICATION_NAME_ENV) is None:
        return
    from application_sdk import (  # noqa: PLC0415 — deferred by convention only; sibling guards import it the same way
        constants,
    )

    configured = constants.APPLICATION_NAME
    if configured == app_name:
        return
    raise AppNameMismatchError(
        message=(
            f"{APPLICATION_NAME_ENV} snapshotted into constants.APPLICATION_NAME "
            f"as {configured!r} but {app_cls.__name__} is registered as "
            f"{app_name!r}. The run's local artifact root is built from the "
            f"former and its task queue and observability tags from the latter, "
            f"so artifacts land under {configured!r} while the run reports "
            f"itself as {app_name!r}."
        ),
        resource=app_cls.__name__,
        expected_state=f"constants.APPLICATION_NAME == {app_name!r}",
        actual_state=f"constants.APPLICATION_NAME == {configured!r}",
        suggested_action=(
            f"Set os.environ.setdefault({APPLICATION_NAME_ENV!r}, {app_name!r}) in "
            "conftest.py, above the application_sdk imports."
        ),
    )


def _verify_infrastructure(expected: InfrastructureContext | None) -> None:
    """Fail when something replaced the kit's infrastructure context before a run.

    :func:`infrastructure` is session-scoped and installs its context globally.
    A per-test fixture that also calls ``set_infrastructure`` — common in suites
    that predate the kit and point the SDK at a per-test ``LocalStore`` — silently
    wins, and the run then reads and writes a store the suite never inspects:
    every artifact assertion fails with "no files", pointing at the App rather
    than at the fixture that moved the store.
    """
    if expected is None:
        return
    from application_sdk.infrastructure import (  # noqa: PLC0415 — deferred: keeps the infrastructure package off the conftest's import path
        get_infrastructure,
    )

    current = get_infrastructure()
    if current is expected:
        return
    raise InfrastructureReplacedError(
        message=(
            "The global infrastructure context is not the one this kit installed, "
            "so the run would use a different object store than the fixtures "
            "expose. Something called set_infrastructure() after the session "
            "fixture — typically a per-test autouse fixture carried over from "
            "before the kit."
        ),
        resource="InfrastructureContext",
        expected_state="the context installed by the kit's infrastructure fixture",
        actual_state="None"
        if current is None
        else f"{type(current).__name__} instance",
        suggested_action=(
            "Have that fixture stand aside for kit tests — e.g. return early when "
            '"executor" is in request.fixturenames — or scope it to the '
            "directory that needs it."
        ),
    )


@contextmanager
def _artifact_preservation(options: KitOptions) -> Iterator[None]:
    """Default the cleanup interceptor off for the block, then restore it.

    Scoped rather than assigned once, because this variable decides whether
    ``App.on_complete()`` deletes a run's artifacts and the whole process reads
    it. A plain ``os.environ[...] = "false"`` from a session fixture leaks into
    every later test in the same process — ``pytest tests/`` runs integration
    and unit tests together, and cleanup-asserting unit tests scheduled after
    this fixture would silently observe cleanup disabled. That is the shape of
    BLDX-1283, which the repo's own ``tests/integration/conftest.py`` restores
    per-test to avoid.

    An explicit environment value always wins and is left untouched; one that
    leaves cleanup *enabled* is warned about, because it silently defeats
    :attr:`KitOptions.preserve_artifacts` and every artifact assertion in the
    adopting suite then fails as "output file missing". Whether a value counts
    as enabled is :func:`_cleanup_enabled`, which mirrors the SDK's own reader —
    a denylist — rather than re-deriving one.

    An empty value is treated as *absent*, not as an explicit choice. The SDK
    reads ``""`` as cleanup-on (it is outside the denylist), so honouring it as
    "the user decided" would disable preservation while suppressing the warning
    that names the cause. ``export VAR=`` and ``env: VAR: ""`` both produce it.
    """
    if not options.preserve_artifacts:
        yield
        return
    current = os.environ.get(CLEANUP_INTERCEPTOR_ENV)
    if current is not None and current.strip():
        if _cleanup_enabled(current):
            logger.warning(
                "%s=%r is set in the environment, so App.on_complete() will "
                "delete each run's output files and this suite's artifact "
                "assertions will fail with 'output file missing'. Unset it, or "
                "set it to 'false', to preserve artifacts as "
                "KitOptions.preserve_artifacts intends.",
                CLEANUP_INTERCEPTOR_ENV,
                current,
            )
        yield
        return
    os.environ[CLEANUP_INTERCEPTOR_ENV] = "false"
    try:
        yield
    finally:
        # ``current`` is None or empty here. Restore an empty value rather than
        # popping it: the block took ownership of the variable, it did not take
        # ownership of whether the variable exists.
        if current is None:
            os.environ.pop(CLEANUP_INTERCEPTOR_ENV, None)
        else:
            os.environ[CLEANUP_INTERCEPTOR_ENV] = current


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
    from application_sdk.common.task_queue import (  # noqa: PLC0415 — deferred: reads constants at call time, after the conftest's setdefault
        task_queue_from_env,
    )

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
def http_fake_source_factory() -> Iterator[HttpFakeSourceFactory]:
    """Session-scoped factory for started :class:`HttpFakeSource` servers.

    The slot a testcontainer fixture occupies, for an HTTP source with no image
    to pull. Build the fake in the connector's ``integration_source`` override,
    register that connector's routes on it, and return it — the kit hands it to
    ``integration_secrets`` and otherwise leaves it alone, exactly as it would a
    container::

        @pytest.fixture(scope="session")
        def integration_source(http_fake_source_factory) -> HttpFakeSource:
            fake = http_fake_source_factory(name="my-source")
            fake.route(r"/api/v1/objects", list_objects)
            return fake

    Every fake built here is stopped when the session ends, and
    ``reset_http_fake_sources`` clears each one's per-test recordings before
    every test. Both arrive with the same star-import as the rest of the kit, so
    a suite cannot pick up the factory and miss the reset.
    """
    global _ACTIVE_FAKE_SOURCE_FACTORY
    factory = HttpFakeSourceFactory()
    _ACTIVE_FAKE_SOURCE_FACTORY = factory
    try:
        yield factory
    finally:
        _ACTIVE_FAKE_SOURCE_FACTORY = None
        factory.stop_all()


@pytest.fixture(autouse=True)
def reset_http_fake_sources() -> None:
    """Reset every session fake's per-test recordings, once a factory is live.

    Autouse and function-scoped, so the pairing with the session factory is
    structural rather than something each test remembers to request. Tests that
    run before the factory is first instantiated have no fakes to reset and pay
    nothing. ``HttpFakeSource.unused_routes()`` reads a lifetime counter this
    does not touch, so the per-suite dead-route assertion still works.
    """
    if _ACTIVE_FAKE_SOURCE_FACTORY is not None:
        _ACTIVE_FAKE_SOURCE_FACTORY.reset_all()


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
def temporary_path(
    tmp_path_factory: pytest.TempPathFactory, integration_options: KitOptions
) -> Iterator[Path]:
    """Point ``constants.TEMPORARY_PATH`` at a session temp dir, and yield it.

    :func:`store_root` covers the object store; this covers the other half a run
    touches — the local scratch tree the SDK builds a run's paths under
    (``{TEMPORARY_PATH}/artifacts/apps/{APPLICATION_NAME}/workflows/...``).
    Left alone it is the repo-relative ``./local/tmp/``, so a suite that asserts
    on run files reads a directory earlier runs also wrote into, and one that
    does not still litters the working tree.

    Not autouse: requesting it is what opts a suite in, exactly as
    ``store_root`` works. Most consumers bind the constant at import time
    (``from application_sdk.constants import TEMPORARY_PATH`` at module top);
    patching ``constants`` alone would miss every one of them already imported.
    The fixture therefore patches ``constants.TEMPORARY_PATH`` — which covers
    modules imported after it runs — and then every already-imported
    ``application_sdk`` module carrying its own binding of the old value. All
    patches are undone on teardown rather than left set. The scan matches by
    value, so a module that stored a *derived* path at import time (say, a
    pre-computed abspath) would be missed — no current consumer does that.

    **The redirect is session-wide, and that reaches other tiers.** Scope is
    ``session`` because the consumers are class-scoped run fixtures, which
    cannot depend on a function-scoped one — so once any test requests this, the
    constant stays redirected for every test that follows in the same process,
    integration and unit alike. ``pytest tests/`` is one process.

    A unit test asserting on a default-rooted path therefore starts failing the
    moment an integration test earlier in the session opts in, and the failure
    names that unit test rather than this fixture. Such a test should pin the
    constant itself instead of leaning on the ambient default::

        monkeypatch.setattr(constants, "TEMPORARY_PATH", "local/tmp")

    Not hypothetical in either direction: it broke two path-normalising unit
    tests in a connector suite, and this repo's own guard tests had to stop
    consuming the fixture — they drive ``__wrapped__`` function-scoped — for the
    same reason.
    """
    from application_sdk import (  # noqa: PLC0415 — deferred by convention only; sibling fixtures import it the same way
        constants,
    )

    path = tmp_path_factory.mktemp(integration_options.temporary_path_prefix)
    previous = constants.TEMPORARY_PATH
    patcher = pytest.MonkeyPatch()
    patcher.setattr(constants, "TEMPORARY_PATH", str(path))
    for name, module in list(sys.modules.items()):
        if (
            name.startswith("application_sdk")
            and module is not None
            and module is not constants
            and getattr(module, "TEMPORARY_PATH", None) == previous
        ):
            patcher.setattr(module, "TEMPORARY_PATH", str(path))
    logger.info(
        "TEMPORARY_PATH redirected to %s for the rest of this session; a later "
        "test asserting on a default-rooted path must pin the constant itself",
        path,
    )
    try:
        yield path
    finally:
        patcher.undo()


@contextmanager
def kit_infrastructure(
    store_root: Path,
    integration_secrets: Mapping[str, str],
    *,
    storage: object | None = None,
) -> Iterator[InfrastructureContext]:
    """The :func:`infrastructure` fixture's body, callable directly.

    Exported because a star-imported fixture cannot be *wrapped*: pytest's
    ``def infrastructure(infrastructure)`` idiom needs a base in an outer scope,
    and the star-import puts this module's fixtures in the adopting conftest's
    own namespace, so that spelling is a ``recursive dependency`` error.
    Overrides therefore fully **replace** rather than extend — and replacing
    used to mean copying this body, including the observability store swap and
    its restore, which is load-bearing rather than incidental.

    So a suite that wants the mocked stores but a real storage backend writes::

        @pytest.fixture(scope="session")
        def infrastructure(store_root, integration_secrets):
            with kit_infrastructure(
                store_root, integration_secrets, storage=my_real_store()
            ) as ctx:
                yield ctx

    Args:
        store_root: Root for the default session ``LocalStore``.
        integration_secrets: Seeded into the ``MockSecretStore``.
        storage: Replaces the default ``create_local_store(store_root)`` when
            given.
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — deferred: keeps infrastructure off the conftest's import path
        InfrastructureContext,
        clear_infrastructure,
        set_infrastructure,
    )
    from application_sdk.observability.observability import (  # noqa: PLC0415 — deferred: observability pulls the storage/binding chain
        AtlanObservability,
    )
    from application_sdk.storage import (  # noqa: PLC0415 — deferred: keeps obstore off the conftest's import path
        create_local_store,
        create_memory_store,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — deferred: same chain as storage above
        BoundStore,
    )
    from application_sdk.testing.mocks import (  # noqa: PLC0415 — deferred: mocks import the infrastructure protocols
        MockSecretStore,
        MockStateStore,
    )

    ctx = InfrastructureContext(
        state_store=MockStateStore(),
        secret_store=MockSecretStore(dict(integration_secrets)),
        storage=storage if storage is not None else create_local_store(store_root),
    )
    previous_store = AtlanObservability._deployment_store
    # Wrapped in a BoundStore — the type this ClassVar already holds in
    # production — so pointing observability at memory needs no widening of a
    # production annotation to accommodate a test.
    AtlanObservability._deployment_store = BoundStore(create_memory_store())
    set_infrastructure(ctx)
    try:
        yield ctx
    finally:
        clear_infrastructure()
        AtlanObservability._deployment_store = previous_store


@pytest.fixture(scope="session")
def infrastructure(
    store_root: Path,
    integration_source: object,
    integration_secrets: Mapping[str, str],
) -> Iterator[InfrastructureContext]:
    """Wire mocked infrastructure for the session, after the source is up.

    Overriding this fixture **replaces** it rather than wrapping it — see
    :func:`kit_infrastructure`, which is this body as a contextmanager so an
    override does not have to copy it. The ``integration_source`` dependency is
    what orders this after the source is up; it is otherwise unused.
    """
    del integration_source
    with kit_infrastructure(store_root, integration_secrets) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def embedded_temporal(
    integration_options: KitOptions,
) -> AsyncIterator[EmbeddedRuntime]:
    """Boot the embedded Temporal dev server for the session."""
    from application_sdk.dev import (  # noqa: PLC0415 — deferred: dev pulls the embedded Temporal/Dapr download machinery
        embedded_runtime,
    )

    async with embedded_runtime(log_level=integration_options.log_level) as runtime:
        yield runtime


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_client(
    embedded_temporal: EmbeddedRuntime,
    integration_app_cls: type[App],
    integration_options: KitOptions,
) -> Client:
    """Connect to the embedded dev server, in its namespace."""
    from application_sdk.execution import (  # noqa: PLC0415 — deferred: keeps temporalio off the conftest's import path
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
    from application_sdk.execution import (  # noqa: PLC0415 — deferred: keeps temporalio off the conftest's import path
        create_worker,
    )

    del infrastructure
    _verify_registration(integration_app_cls)
    with _artifact_preservation(integration_options):
        async with create_worker(temporal_client, task_queue=integration_task_queue):
            yield


@pytest.fixture(scope="session")
def executor(
    temporal_client: Client,
    worker: None,
    integration_task_queue: str,
    infrastructure: InfrastructureContext,
) -> AppExecutor:
    """Executor submitting to the running worker's task queue.

    Carries the installed infrastructure context so each run can check it is
    still the live one — see :func:`_verify_infrastructure`.
    """
    from application_sdk.execution import (  # noqa: PLC0415 — deferred: keeps temporalio off the conftest's import path
        TemporalExecutorBackend,
    )

    del worker
    return AppExecutor(
        backend=TemporalExecutorBackend(
            client=temporal_client, task_queue=integration_task_queue
        ),
        expected_infrastructure=infrastructure,
    )


__all__ = [
    "APPLICATION_NAME_ENV",
    "CLEANUP_INTERCEPTOR_ENV",
    "DEPLOYMENT_NAME_ENV",
    "AppExecutor",
    "KitOptions",
    "embedded_temporal",
    "executor",
    "http_fake_source_factory",
    "infrastructure",
    "integration_app_cls",
    "integration_options",
    "integration_secrets",
    "integration_source",
    "integration_task_queue",
    "kit_infrastructure",
    "reset_http_fake_sources",
    "store_root",
    "temporal_client",
    "temporary_path",
    "worker",
]
