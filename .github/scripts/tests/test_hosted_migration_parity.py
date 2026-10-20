"""Conformance P061 and ``gen_app_api.py check`` agree on the cases they share.

P061 (``conformance.suite.checks.hosted_handler.migration``) ships in the
conformance wheel, which runs in app repos and cannot import this script, so it
carries its own copy of ``check``'s handler-misuse and ``compute_manifest``
logic. This pins the two together: on each fixture both flag it, or neither.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conformance.suite.checks.hosted_handler import migration

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gen_app_api as gen  # noqa: E402

_PYPROJECT = """
[project]
name = "atlan-demo-app"
version = "1.0.0"
dependencies = ["atlan-application-sdk[sql]>=3.49.1"]

[tool.atlan-app-api]
handler = "app.handler:DemoHandler"
data = ["app/sql/*.sql"]
"""

_HANDLER = (
    "from application_sdk.handler import Handler\n"
    "from .client import C\n\n\n"
    "class DemoHandler(Handler):\n"
    "    ...\n"
)

_BASE = {
    "app/handler.py": _HANDLER,
    "app/client.py": "from .failures import Boom\nclass C: ...\n",
    "app/failures.py": "class Boom(Exception): ...\n",
    "app/sql/check.sql": "SELECT 1\n",
}


def _app(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    for rel, text in {**_BASE, **files}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


CASES = {
    "clean": {},
    "absolute app import": {"app/client.py": "from app.failures import Boom\n"},
    "run_in_thread moved": {
        "app/client.py": "from .failures import Boom\n"
        "from application_sdk.execution.heartbeat import run_in_thread\n"
    },
    "get_logger moved": {
        "app/client.py": "from .failures import Boom\n"
        "from application_sdk.observability.logger_adaptor import get_logger\n"
    },
    "APPLICATION_NAME read": {
        "app/client.py": "import os\nN = os.environ['ATLAN_APPLICATION_NAME']\n"
    },
    "APPLICATION_NAME outside the closure": {
        "app/worker.py": "N = 'APPLICATION_NAME'\n"
    },
    "compute_manifest hook": {
        "app/extract/core.py": "async def compute_manifest(m, fe):\n    return m\n"
    },
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_p061_and_check_agree(tmp_path: Path, case: str) -> None:
    root = _app(tmp_path, CASES[case])
    check_flags = bool(gen.check(root, gen.load(root)))
    p061_flags = bool(migration.scan_repo(root))
    assert check_flags == p061_flags, case
    assert check_flags is (
        case not in {"clean", "APPLICATION_NAME outside the closure"}
    )


def test_the_moved_imports_match() -> None:
    assert migration.KNOWN_MOVES == gen.KNOWN_MOVES
