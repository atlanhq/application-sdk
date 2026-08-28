"""The shared integration fixtures, exercised the way a connector adopts them.

Every other test of ``application_sdk.testing.integration.fixtures`` asserts on
the fixture *functions* — their signatures, their dependency parameters. That
proves the graph is wired as intended and nothing about whether pytest can
resolve it: fixture scopes, ``loop_scope="session"`` on three async fixtures, a
sync ``executor`` depending on an async ``worker``, and a star-import whose
names must land in the conftest namespace for pytest to see them at all. Those
only fail inside a real session.

So this runs one — as a subprocess, in a fresh interpreter, because the shape
under test *is* the import ordering: ``os.environ.setdefault`` above the
``application_sdk`` imports, which this process passed long ago during
collection. The conftest below is the one
``docs/guides/integration-fixtures.md`` tells connectors to copy; if that
listing stops working, this test is what says so.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[3]

_APP_MODULE = '''
"""A minimal registered App, at module level so Temporal's sandbox can import it."""

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output


class EchoInput(Input):
    value: int = 0


class EchoOutput(Output):
    result: int = 0


class EchoApp(App):
    @task
    async def add_one(self, input: EchoInput) -> EchoOutput:
        return EchoOutput(result=input.value + 1)

    async def run(self, input: EchoInput) -> EchoOutput:
        return await self.add_one(input)
'''

# Verbatim the shape documented in docs/guides/integration-fixtures.md.
_CONFTEST = """
import os

os.environ.setdefault("ATLAN_APPLICATION_NAME", "kitsmoke")
os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

import pytest  # noqa: E402

from application_sdk.testing.integration.fixtures import *  # noqa: E402, F403

from kit_smoke_app import EchoApp  # noqa: E402


@pytest.fixture(scope="session")
def integration_app_cls():
    return EchoApp
"""

_TEST = """
import os

import pytest

from application_sdk.common.task_queue import task_queue_from_env
from kit_smoke_app import EchoApp, EchoInput


@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_runs_through_the_kit(executor):
    output = await executor.execute_app(EchoApp, EchoInput(value=41))
    assert output.result == 42
    # KitOptions.preserve_artifacts, observed from inside the worker fixture's
    # lifetime — the only place the default exists, since the prior value is
    # restored on teardown. _clean_env() strips APPLICATION_SDK_ENABLE_* from
    # the child environment (pinned by its own test), so a pass here can only
    # come from the kit's own default.
    # It has to ride this async test: a sync one cannot request `worker`.
    #
    # os.getenv, not os.environ[...]: a subscript makes the mapping the subject
    # of the failing expression, so pytest renders the whole environment - and
    # this child inherits the developer's - into the report, which the outer
    # test then re-emits via its own assertion message.
    cleanup_interceptor = os.getenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR")
    assert cleanup_interceptor == "false", (
        f"kit default not applied: {cleanup_interceptor!r}"
    )


def test_the_queue_is_the_deployment_queue(integration_task_queue):
    # Not "kitsmoke-queue": the kit derives what the real deployment polls.
    assert integration_task_queue == task_queue_from_env() == "atlan-kitsmoke-ci"


def test_mocked_infrastructure_is_installed(infrastructure):
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.testing.mocks import MockSecretStore, MockStateStore

    assert get_infrastructure() is infrastructure
    assert isinstance(infrastructure.state_store, MockStateStore)
    assert isinstance(infrastructure.secret_store, MockSecretStore)
"""

# A suite whose App never reaches the registry — the precondition the module
# docstring of fixtures.py calls load-bearing ("unregistered App fails before
# create_worker snapshots an empty registry"). Overriding integration_app_cls
# is what reaches that call: the default fixture raises on the *parameter*,
# before the worker body runs.
_UNREGISTERED_CONFTEST = '''
import os

os.environ.setdefault("ATLAN_APPLICATION_NAME", "kitsmoke")
os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

import pytest  # noqa: E402

from application_sdk.app.base import App  # noqa: E402

from application_sdk.testing.integration.fixtures import *  # noqa: E402, F403


class NeverRegisteredApp(App):
    """Subclasses App without the registration that stamps ``_app_name``."""


@pytest.fixture(scope="session")
def integration_app_cls():
    return NeverRegisteredApp


@pytest.fixture(scope="session")
def temporal_client():
    # The guard under test runs before create_worker is handed this, so the
    # case need not pay ~30s to boot a dev server it will never reach. If the
    # guard is ever moved below create_worker, this stub makes the test fail
    # rather than quietly pass.
    return object()
'''

_UNREGISTERED_TEST = """
import pytest


@pytest.mark.asyncio(loop_scope="session")
async def test_the_worker_is_built(worker):
    raise AssertionError("the worker fixture should have refused to build")
"""

_INI = """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = session
asyncio_default_test_loop_scope = session
"""


@pytest.fixture
def adopting_suite(tmp_path: Path) -> Path:
    """A connector suite laid out exactly as the adoption guide prescribes."""
    (tmp_path / "kit_smoke_app.py").write_text(_APP_MODULE)
    (tmp_path / "pytest.ini").write_text(_INI)
    suite = tmp_path / "tests" / "integration"
    suite.mkdir(parents=True)
    (suite / "conftest.py").write_text(_CONFTEST)
    (suite / "test_kit.py").write_text(_TEST)
    return tmp_path


def _run_pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        # Embedded Temporal fetches its binary to ~/.cache on first use.
        timeout=600,
        env={
            **_clean_env(),
            "PYTHONPATH": f"{cwd}{os.pathsep}{_REPO_ROOT}",
        },
    )


def _clean_env() -> dict[str, str]:
    """The ambient environment minus the vars the conftest is meant to own.

    The parent may have ``ATLAN_*`` set — a developer's ``.env`` commonly does,
    and ``tests/integration/conftest.py`` sets the ``APPLICATION_SDK_ENABLE_*``
    ones autouse for every integration test, this file included. Inheriting
    either would mask a conftest that never sets them, or let a child assertion
    pass on the parent's value instead of the kit's own default. Pinned by
    :func:`test_clean_env_drops_the_vars_the_conftest_must_own`, because a strip
    nothing asserts on is a strip that can be deleted with this suite green.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"ATLAN_APPLICATION_NAME", "ATLAN_DEPLOYMENT_NAME"}
        and not k.startswith("APPLICATION_SDK_ENABLE_")
    }


def test_clean_env_drops_the_vars_the_conftest_must_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_clean_env`` is the premise of the child assertions; pin it.

    ``test_star_import_suite_passes_end_to_end``'s cleanup-interceptor assertion
    is only meaningful because the child cannot inherit that variable — and the
    parent really does set it: ``tests/integration/conftest.py`` sets it autouse
    for every integration test, this one included. Without the strip the child
    inherits ``"false"``, ``_artifact_preservation`` takes its "explicit value
    wins" branch, and the assertion passes on the inherited value instead of the
    kit's default — so the guard it exists to pin could then be deleted with
    this whole suite green.

    Asserts on the leaked *key names* only. ``assert "X" not in _clean_env()``
    would render the whole mapping into the failure output, and this dict is the
    developer's environment.
    """
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "parent")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "parent")
    monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")
    leaked = sorted(
        k
        for k in _clean_env()
        if k in {"ATLAN_APPLICATION_NAME", "ATLAN_DEPLOYMENT_NAME"}
        or k.startswith("APPLICATION_SDK_ENABLE_")
    )
    assert leaked == [], f"_clean_env forwarded to the child: {leaked}"


def test_star_import_suite_passes_end_to_end(adopting_suite: Path) -> None:
    result = _run_pytest(adopting_suite, "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


def test_missing_app_cls_override_is_reported(adopting_suite: Path) -> None:
    """Without the one required override, the failure names the fixture."""
    conftest = adopting_suite / "tests" / "integration" / "conftest.py"
    conftest.write_text(
        conftest.read_text().split('@pytest.fixture(scope="session")')[0]
    )
    result = _run_pytest(adopting_suite, "-q", "-k", "queue")
    assert result.returncode != 0
    assert "integration_app_cls" in result.stdout + result.stderr


def test_env_set_after_the_import_fails_collection(adopting_suite: Path) -> None:
    """The ordering rule the module docstring calls deliberately loud."""
    conftest = adopting_suite / "tests" / "integration" / "conftest.py"
    conftest.write_text(
        conftest.read_text().replace(
            'os.environ.setdefault("ATLAN_APPLICATION_NAME", "kitsmoke")',
            "import application_sdk.constants  # too early\n"
            'os.environ["ATLAN_APPLICATION_NAME"] = "kitsmoke"',
        )
    )
    result = _run_pytest(adopting_suite, "-q")
    assert result.returncode != 0
    assert "IntegrationEnvOrderingError" in result.stdout + result.stderr


def test_the_worker_fixture_refuses_an_unregistered_app(
    adopting_suite: Path,
) -> None:
    """The worker fixture's own precondition, asserted through the fixture.

    ``test_unregistered_app_fails_before_the_worker_is_built`` in the unit
    suite exercises ``_verify_registration`` directly, and
    ``test_missing_app_cls_override_is_reported`` above fails on the fixture
    parameter without ever reaching the worker body. Neither holds the call
    site itself to anything: deleting line 519 of ``fixtures.py`` leaves both
    green, and the worker then starts against an empty registry and fails
    every workflow task it is handed.
    """
    suite = adopting_suite / "tests" / "integration"
    (suite / "conftest.py").write_text(_UNREGISTERED_CONFTEST)
    (suite / "test_kit.py").write_text(_UNREGISTERED_TEST)
    result = _run_pytest(adopting_suite, "-q")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "AppRegistrationMissingError" in output, output
    assert "not in the App registry" in output, output
