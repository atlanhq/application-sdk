"""Unit test configuration and autouse fixtures."""

import importlib.util
import os
import time
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hypothesis import settings
from loguru import logger as _loguru_logger

from application_sdk._runtime.progress import (
    ClosedHold,
    ProgressTracker,
    bind_progress_tracker,
)

# Re-export shared fixtures so all unit tests can use them without explicit
# per-file imports (pytest discovers fixtures via the conftest chain).
# ``restore_logger_init_flags`` is autouse: the import is what applies it.
from application_sdk.testing.fixtures import (  # noqa: F401
    clean_app_registry,
    clean_task_registry,
    restore_logger_init_flags,
)
from tests.unit.private_stdlib import private_stdlib

# One hypothesis profile for the whole unit tree, loaded once at collection. A
# module-level ``settings.load_profile`` in a test file replaces it for every
# test the xdist worker runs after that module is imported, so which deadline a
# property test gets depended on which files shared its worker. No deadline:
# a single slow example on a loaded runner is not a failure.
settings.register_profile("unit", deadline=None)
settings.load_profile("unit")

# Snapshot at conftest import time: collection happens before any test runs, so
# this is the driver's pristine mapping.
if importlib.util.find_spec("psycopg2") is not None:
    import psycopg2.extensions as _psycopg2_ext

    _PRISTINE_PSYCOPG2_ENCODINGS: dict[str, str] | None = dict(_psycopg2_ext.encodings)
else:  # psycopg2 is a test-only dep; tolerate interpreters without a wheel
    _psycopg2_ext = None  # type: ignore[assignment]  # — sentinel for "driver not installed": None is not a module
    _PRISTINE_PSYCOPG2_ENCODINGS = None


class RecordingProgressTracker(ProgressTracker):
    """A real :class:`ProgressTracker` that also keeps a history of its signals.

    ``ProgressTracker`` only retains the *most recent* label, which is all the
    stall watchdog needs but not enough to assert that a framework hook fires
    once per unit of work rather than once per record (ADR-0018's hard
    constraint). Subclassing keeps the real stall/hold behaviour intact — no
    stub — while adding the ordered histories a hook or auto-hold test needs.

    :attr:`labels` records ``mark_progress`` calls; :attr:`holds` records every
    hold as it closes, which is a separate list because a closed hold is *not* a
    ``mark_progress`` call — ``exit_hold`` re-arms the stall clock directly, so a
    hold would otherwise leave no trace in either history.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.holds: list[ClosedHold] = []
        super().__init__(clock=clock, on_hold_closed=self.holds.append)
        self.labels: list[str] = []

    def mark_progress(self, label: str = "") -> None:
        self.labels.append(label)
        super().mark_progress(label)

    def count(self, label: str) -> int:
        """How many times *label* was recorded."""
        return self.labels.count(label)

    def holds_for(self, label: str) -> list[ClosedHold]:
        """Every closed hold recorded under *label*, in the order they closed."""
        return [hold for hold in self.holds if hold.label == label]


@pytest.fixture
def progress_marks() -> Iterator[RecordingProgressTracker]:
    """Bind a recording tracker for the test, and hand it back.

    Framework hooks read the current attempt's tracker via
    :func:`~application_sdk.execution.progress.current_progress_tracker`, which
    outside an activity returns the inert tracker that discards every signal —
    so binding a real one here is what turns the hooks on for a test. Stands in
    for the per-attempt bind ``activities.py`` does in production, via the same
    ``bind_progress_tracker`` block, so a test can never leave a binding behind.
    """
    tracker = RecordingProgressTracker()
    with bind_progress_tracker(tracker):
        yield tracker


@pytest.fixture
def loguru_capture():
    """Capture loguru log records emitted during the test.

    Yields a list of raw loguru ``record`` dicts (same structure as
    ``message.record`` in a loguru sink).  Extra fields bound via
    ``logger.bind(**kwargs)`` are available under ``record["extra"]``.
    """
    records: list[dict] = []
    sink_id = _loguru_logger.add(
        lambda message: records.append(message.record),
        level="DEBUG",
        format="{message}",
    )
    yield records
    try:
        _loguru_logger.remove(sink_id)
    except ValueError:
        # Someone already took this sink out from under us: an adapter
        # initialised mid-test and loguru's remove-all took every handler with
        # it (see _restore_logger_init_flags). That fixture is what stops the
        # cross-test case; this only keeps a test that resets the flags itself
        # from dying in teardown. Note the capture is *incomplete* when this
        # fires — anything logged after the wipe never reached ``records``.
        pass


def _safe_patch(target, side_effect=None, mock_obj=None):
    """Create a patch context that gracefully handles unresolvable targets."""
    try:
        if mock_obj is not None:
            ctx = patch(target, mock_obj)
        elif side_effect is not None:
            ctx = patch(target, side_effect=side_effect)
        else:
            ctx = patch(target)
        ctx.__enter__()
        return ctx
    except (AttributeError, ModuleNotFoundError):
        return None


@pytest.fixture(autouse=True)
def _no_post_drain_exit_deadline(monkeypatch):
    """Disable the post-drain exit deadline unless a test turns it on.

    Run modes start it after the worker drains, and its expiry calls
    ``os._exit``: left on, a test that runs a mode would end the pytest
    process two minutes later.
    """
    monkeypatch.setenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", "0")


@pytest.fixture(autouse=True)
def _hermetic_memory_limit(monkeypatch):
    """Keep the host's cgroup limit out of every unit test, and reset the
    process-wide memory-pressure throttle.

    ``cgroup.memory_limit_bytes()`` reads the cgroup before
    ``K8S_POD_MEMORY_LIMIT``, so on a runner inside a memory-capped container
    the host limit would win over a test's ``setenv``. Emptying the limit paths
    makes the env var the only source unless a test sets the paths itself.
    ``heartbeat._memory_warn_state`` and ``heartbeat._memory_limit_cache`` are
    module globals, so without a reset one test's warning would throttle the
    next test's, and one test's limit would leak into the next.
    """
    from application_sdk.execution import heartbeat
    from application_sdk.observability import cgroup

    monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", ())
    monkeypatch.setattr(heartbeat, "_memory_warn_state", heartbeat._MemoryWarnState())
    monkeypatch.setattr(heartbeat, "_memory_limit_cache", heartbeat._MemoryLimitCache())


@pytest.fixture(autouse=True)
def _reset_dapr_sidecar_cold_start_gate(monkeypatch):
    """Reset the process-level Dapr cold-start gates before every unit test.

    ``application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready``
    (holistic, healthz-confirmed) and ``_dapr_component_confirmed_ready``
    (per-component, set by ``retry_past_dapr_cold_start`` callers — agent
    bundle fetch, single-key probes, the named-credential resolver path, the
    GUID/vault credential and config-fetch paths) are both module globals.
    Without a reset, a successful resolve in one test would leave them set
    for the rest of the session, silently skipping the cold-start retry loop
    under test in a later, order-dependent test.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready", False
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http._dapr_component_confirmed_ready",
        set(),
    )


@pytest.fixture(autouse=True)
def _reset_fetched_binding_secrets_registry():
    """Reset the startup-fetched binding-secrets registry after every unit test.

    ``application_sdk.storage.binding._FETCHED_BINDING_SECRETS`` is a
    process-wide dict populated by ``_create_infrastructure`` via
    ``set_fetched_binding_secrets``.  A test that exercises the startup wiring
    with the real setter (e.g.
    ``test_main_binding_secrets.py::test_infrastructure_passes_the_fetched_secrets_to_the_resolver``)
    leaves entries behind, and a later test that expects env-only resolution
    then sees the leaked secret map and fails — order-dependent, green in
    isolation.  Same hazard class as ``_reset_dapr_sidecar_cold_start_gate``
    above; same fix shape.
    """
    yield
    from application_sdk.storage.binding import (
        _reset_fetched_binding_secrets,
    )

    _reset_fetched_binding_secrets()


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """Put ``os.environ`` back as the test found it.

    ``monkeypatch.delenv(name, raising=False)`` on an absent variable records
    nothing, so a variable the code under test then sets with
    ``os.environ.setdefault`` outlives the test; so does anything set or popped
    directly. Restoring by difference rather than ``clear()`` + ``update()``
    means a thread still running from this test never sees an empty
    environment. Order against ``monkeypatch`` does not matter: each restores
    only what it recorded.
    """
    before = dict(os.environ)
    yield
    for name in os.environ.keys() - before.keys():
        os.environ.pop(name, None)
    for name, value in before.items():
        if os.environ.get(name) != value:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def reset_registries(
    clean_app_registry,  # noqa: F811 — the imported fixture, requested by name
    clean_task_registry,  # noqa: F811 — the imported fixture, requested by name
) -> None:
    """Reset ``AppRegistry`` and ``TaskRegistry`` around every unit test.

    Both are process-wide. Defining an ``App`` subclass registers it, so
    without a reset one test's apps and tasks are visible to whichever test
    the xdist worker runs next. Requests the shared ``clean_*_registry``
    fixtures rather than repeating their resets, so a test that also asks for
    one by name gets the same instance.
    """


@pytest.fixture(autouse=True)
def _clear_infrastructure() -> Iterator[None]:
    """Clear the process-wide infrastructure context around every unit test.

    ``set_infrastructure`` writes a module global that ``get_infrastructure``
    reads everywhere; a test that sets it and never clears it hands its mocks
    to the next test on the worker.
    """
    from application_sdk.infrastructure.context import clear_infrastructure

    clear_infrastructure()
    yield
    clear_infrastructure()


@pytest.fixture(autouse=True)
def _reset_sizing_sink() -> Iterator[None]:
    """Drop the lazily built, process-wide sizing sink around every unit test."""
    from application_sdk.observability import sizing_sink

    sizing_sink._reset_for_testing()
    yield
    sizing_sink._reset_for_testing()


@pytest.fixture(autouse=True)
def restore_psycopg2_encodings() -> Iterator[None]:
    """Undo any process-global rewrite of psycopg2's encodings map.

    ``install_tolerant_connection_decoder`` rewrites
    ``psycopg2.extensions.encodings`` in place — deliberately, because that dict
    is the only Python-reachable seam for psycopg2's connection decoder (see
    ``application_sdk.clients.sql_typecasters``). Any test that builds a SQL
    engine, in any directory, leaks it into every test that runs afterwards on
    the worker, which would make ``TestRealPsycopg2Premises`` pass or fail by
    ordering.
    """
    yield
    if _PRISTINE_PSYCOPG2_ENCODINGS is None or _psycopg2_ext is None:
        return
    if _psycopg2_ext.encodings != _PRISTINE_PSYCOPG2_ENCODINGS:
        _psycopg2_ext.encodings.clear()
        _psycopg2_ext.encodings.update(_PRISTINE_PSYCOPG2_ENCODINGS)


@pytest.fixture
def fast_dapr_cold_start_retry(monkeypatch):
    """Zero out cold-start retry backoff so a retry-then-succeed test runs
    instantly instead of sleeping for real between attempts.

    Shared by every ``retry_past_dapr_cold_start`` call site's "retries a
    transient failure then succeeds" test (agent bundle fetch, single-key
    probes, the named-credential resolver path, the GUID/vault credential
    and config-fetch paths) — previously each duplicated the same three
    ``monkeypatch.setattr`` calls.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        30.0,
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_BASE_DELAY_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_DELAY_SECONDS",
        0.0,
    )


@pytest.fixture
def deterministic_dapr_cold_start_deadline(monkeypatch):
    """Deterministic fake clock + no-op sleep for a "gives up at the
    deadline" cold-start-retry test.

    Each attempt advances the fake clock by 6s against a 10s budget, so the
    retry loop gives up after exactly 2 attempts regardless of real
    wall-clock scheduling delays under load — a real-time-based loose
    ``>= 2`` assertion would flake on a contended runner. Shared by every
    ``retry_past_dapr_cold_start`` call site's deadline-exhaustion test;
    previously each duplicated the same fake-clock + mocked-sleep setup.
    """
    from application_sdk.infrastructure._dapr import http as dapr_http

    monkeypatch.setattr(dapr_http, "DAPR_COLD_START_MAX_WAIT_SECONDS", 10.0)
    fake_now = {"t": 0.0}

    def fake_monotonic() -> float:
        fake_now["t"] += 6.0
        return fake_now["t"]

    # Private copies, so the fake clock and the no-op sleep reach only the
    # retry loop under test, not the event loop or another test's thread.
    with (
        private_stdlib(dapr_http, "asyncio") as local_asyncio,
        private_stdlib(dapr_http, "time") as local_time,
    ):
        monkeypatch.setattr(local_asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(local_time, "monotonic", fake_monotonic)
        yield


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock get_deployment_secret for all unit tests."""
    ctx = _safe_patch(
        "application_sdk.infrastructure.secrets.get_deployment_secret",
        side_effect=lambda key, **_: None,
    )
    yield
    if ctx is not None:
        ctx.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def mock_dapr_client():
    """Automatically mock DaprClient for all unit tests to prevent Dapr health check timeouts."""

    def _make_mock_dapr():
        mock_instance = Mock()
        mock_instance.publish_event = Mock()
        mock_instance.invoke_binding = Mock()
        mock_instance.get_state = Mock(return_value=Mock(data=None))
        mock_instance.save_state = Mock()
        mock_instance.get_secret = Mock(return_value=Mock(secret={}))
        return mock_instance

    mock_dapr = Mock()
    mock_instance = _make_mock_dapr()
    mock_dapr.return_value.__enter__ = Mock(return_value=mock_instance)
    mock_dapr.return_value.__exit__ = Mock(return_value=None)

    ctx = _safe_patch(
        "application_sdk.infrastructure._dapr.client.DaprClient",
        mock_obj=mock_dapr,
    )
    yield
    if ctx is not None:
        ctx.__exit__(None, None, None)


#: Mirrors ``components/objectstore.yaml`` except for the root, which the
#: fixture below fills in.
_LOCAL_OBJECTSTORE_COMPONENT = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: objectstore
spec:
  version: v1
  type: bindings.localstorage
  ignoreErrors: true
  metadata:
    - name: rootPath
      value: "{root}"
"""


@pytest.fixture(scope="session", autouse=True)
def local_writes_stay_out_of_the_repo(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Root every default-located write a unit test can make under ``tmp_path``.

    Left alone, two defaults resolve against the working directory — the repo
    — and are shared by every xdist worker at once:

    - ``TEMPORARY_PATH`` (``./local/tmp/``): run scratch trees and the
      observability sinks' data dirs. Redirected through the env var as well as
      the module bindings, because the env var is the only thing a subprocess
      or spawn-pool child sees — ``testing/integration/test_fixtures.py`` runs
      fresh interpreters that import the SDK.
    - ``DAPR_COMPONENTS_PATH`` (``./components``): the observability sinks
      resolve the deployment object store from it, and the repo's
      ``objectstore.yaml`` roots that store at ``./local/dapr/objectstore``.

    Session-scoped, so it is in place before the first test builds a
    process-wide sink or store, and one directory per xdist worker, because
    ``tmp_path_factory`` already is. ``.github/scripts/check_test_write_hygiene.py``
    fails the CI leg if any write still lands in the checkout. Hypothesis's
    storage is moved in ``tests/conftest.py`` instead: it is written before any
    fixture runs.

    What no fixture can reach: importing the SDK builds the default logger,
    whose constructor creates its (empty) observability directory under
    ``./local/tmp/``. Git does not report an empty directory, so the check above
    stays green; a file written there would not.
    """
    from application_sdk.testing.fixtures import _redirect_temporary_path

    root = tmp_path_factory.mktemp("sdk-local")
    temporary = root / "tmp"
    components = root / "components"
    components.mkdir()
    (components / "objectstore.yaml").write_text(
        _LOCAL_OBJECTSTORE_COMPONENT.format(root=(root / "objectstore").as_posix()),
        encoding="utf-8",
    )

    patcher = pytest.MonkeyPatch()
    patcher.setenv("ATLAN_TEMPORARY_PATH", str(temporary))
    patcher.setenv("DAPR_COMPONENTS_PATH", str(components))
    _redirect_temporary_path(patcher, str(temporary))
    try:
        yield
    finally:
        patcher.undo()


@pytest.fixture
def production_temporary_path(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``TEMPORARY_PATH`` back to its production default, ``./local/tmp/``.

    For a test about that spelling — a ``./local/tmp/...`` workflow path
    normalising to its object-store key — which the session-wide redirect above
    would otherwise turn into a path outside the root. A test that writes files
    should build them under the live constant instead; this one would put them
    in the repo.
    """
    from application_sdk.testing.fixtures import _redirect_temporary_path

    _redirect_temporary_path(monkeypatch, "./local/tmp/")
    return "./local/tmp/"


@pytest.fixture(autouse=True)
def e2e_evidence_stays_out_of_the_repo(_e2e_evidence_root, monkeypatch):
    """Point the e2e evidence bundle at a temp directory for every unit test.

    ``BaseE2ETest.evidence_dir`` defaults to ``results/e2e-evidence``, relative
    to the working directory — which is right in CI, where ``results/`` is what
    ``upload-artifact`` is pointed at, and wrong here, where the working
    directory is the repo. Without this, any unit test that drives
    ``test_full_dag_runs_end_to_end`` to a failure writes real files into the
    checkout, and the first sign of it is an untracked directory in ``git
    status`` rather than a failing assertion.

    Autouse and class-level rather than a per-test opt-in, because the tests
    that trip it are the ones *not* about evidence: they drive the failure path
    for some unrelated reason and have no cause to know a bundle now exists. A
    test that is about the bundle sets ``evidence_dir`` on its own instance,
    which still wins.

    The directory is made once per xdist worker, not once per test: a
    ``mktemp`` on every one of ~13k tests was the largest fixed per-test cost
    in the unit tier (FND-4304). Nothing reads a bundle it did not write
    itself, so sharing the directory changes nothing a test can observe; the
    attribute is still patched per test, so a test that reassigns it is still
    undone.
    """
    from application_sdk.testing.e2e.base import BaseE2ETest

    monkeypatch.setattr(BaseE2ETest, "evidence_dir", _e2e_evidence_root)


@pytest.fixture(scope="session")
def _e2e_evidence_root(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One e2e evidence directory per xdist worker, shared by its tests."""
    return str(tmp_path_factory.mktemp("e2e-evidence"))
