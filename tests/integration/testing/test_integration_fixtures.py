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
import pytest

from application_sdk.common.task_queue import task_queue_from_env
from kit_smoke_app import EchoApp, EchoInput


@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_runs_through_the_kit(executor):
    output = await executor.execute_app(EchoApp, EchoInput(value=41))
    assert output.result == 42


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

    The parent process is itself a pytest run with ``ATLAN_*`` set; inheriting
    those would mask a conftest that never sets them.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"ATLAN_APPLICATION_NAME", "ATLAN_DEPLOYMENT_NAME"}
        and not k.startswith("APPLICATION_SDK_ENABLE_")
    }


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
