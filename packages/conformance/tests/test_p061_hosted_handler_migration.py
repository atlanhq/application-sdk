"""P061 HostedHandlerMigration — what still stands between an app and the host.

The compliant fixture mirrors the canonical reference (atlan-mysql-app #778):
a ``[tool.atlan-app-api]`` block naming a ``Handler`` subclass in ``app/`` whose
files import each other relatively, and no ``server/`` package. Each test then
breaks one thing and expects exactly the finding for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks import hosted_handler
from conformance.suite.checks.hosted_handler import migration
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_BLOCK = """
[tool.atlan-app-api]
handler = "app.handler:MySQLAppHandler"
data = ["app/sql/*.sql"]
dependencies = ["aiomysql>=0.3.0"]
extras = ["sql"]
"""

_PYPROJECT = """[project]
name = "atlan-mysql-app"
version = "2.4.0"
dependencies = ["atlan-application-sdk[sql]>=3.49.1"]
"""

_HANDLER = """from application_sdk.handler import Handler

from .client import SQLClient


class MySQLAppHandler(Handler):
    async def test_auth(self, input):
        return await SQLClient().ping()
"""

_FILES = {
    "app/handler.py": _HANDLER,
    "app/client.py": "from .failures import Boom\n\n\nclass SQLClient:\n    ...\n",
    "app/failures.py": "class Boom(Exception): ...\n",
    "app/worker.py": "import temporalio\nAPP = 'ATLAN_APPLICATION_NAME'\n",
    "app/sql/test_authentication.sql": "SELECT 1\n",
}


def _app(
    tmp_path: Path,
    *,
    block: str = _BLOCK,
    files: dict[str, str] | None = None,
) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT + block)
    for rel, text in {**_FILES, **(files or {})}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _findings(root: Path) -> list[tuple[str, int, str]]:
    return [
        (f.file, f.line, f.message)
        for f in migration.scan_repo(root)
        if not f.suppressed
    ]


def test_the_canonical_shape_is_clean(tmp_path: Path) -> None:
    assert _findings(_app(tmp_path)) == []


def test_an_unmigrated_app_fires_on_its_handler_class(tmp_path: Path) -> None:
    findings = _findings(_app(tmp_path, block=""))
    assert [(f, line) for f, line, _ in findings] == [("app/handler.py", 6)]
    assert (
        "MySQLAppHandler is not served from the consolidated API host"
        in (findings[0][2])
    )
    assert "api-server-consolidation-migration" in findings[0][2]


def test_a_server_sdk_copy_fires_even_when_migrated(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        files={
            "server/pyproject.toml": (
                '[project]\nname = "atlan-mysql-server"\n'
                'dependencies = [\n  "atlan-server-sdk @ git+https://x",\n]\n'
            )
        },
    )
    findings = _findings(root)
    assert [(f, line) for f, line, _ in findings] == [("server/pyproject.toml", 4)]
    assert "delete server/ once the host's pin has moved" in findings[0][2]


def test_a_server_folder_without_server_sdk_is_not_the_old_path(
    tmp_path: Path,
) -> None:
    root = _app(tmp_path, files={"server/pyproject.toml": '[project]\nname = "x"\n'})
    assert _findings(root) == []


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            _BLOCK.replace("app.handler:MySQLAppHandler", "app.handler:Missing"),
            "does not name a class in app/",
        ),
        (
            _BLOCK.replace("app.handler:MySQLAppHandler", "handler:MySQLAppHandler"),
            "must be 'app.<module>:<Class>'",
        ),
        (_BLOCK.replace("app/sql/*.sql", "app/nope/*.sql"), "matches nothing"),
    ],
)
def test_a_block_that_does_not_resolve_fires(
    tmp_path: Path, block: str, expected: str
) -> None:
    findings = _findings(_app(tmp_path, block=block))
    assert len(findings) == 1
    assert findings[0][0] == "pyproject.toml"
    assert expected in findings[0][2]


def test_a_handler_class_that_is_not_a_handler_fires(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        files={"app/handler.py": "class MySQLAppHandler:\n    ...\n"},
    )
    findings = _findings(root)
    assert len(findings) == 1
    assert "subclasses the SDK's Handler" in findings[0][2]


@pytest.mark.parametrize(
    ("client", "line", "expected"),
    [
        (
            "from app.failures import Boom\n",
            1,
            "Absolute import of app.failures",
        ),
        (
            "from .failures import Boom\n"
            "from application_sdk.execution.heartbeat import run_in_thread\n",
            2,
            "is absent on the host",
        ),
        (
            "import os\nfrom .failures import Boom\n"
            "NAME = os.environ['ATLAN_APPLICATION_NAME']\n",
            3,
            "APPLICATION_NAME is the host's own name",
        ),
    ],
)
def test_misuse_in_the_handlers_closure_fires(
    tmp_path: Path, client: str, line: int, expected: str
) -> None:
    findings = _findings(_app(tmp_path, files={"app/client.py": client}))
    assert [(f, ln) for f, ln, _ in findings] == [("app/client.py", line)]
    assert expected in findings[0][2]


def test_files_outside_the_closure_are_not_read(tmp_path: Path) -> None:
    """app/worker.py reads ATLAN_APPLICATION_NAME but the handler never imports it."""
    assert _findings(_app(tmp_path)) == []


def test_a_compute_manifest_hook_fires(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        files={
            "app/extract/core.py": (
                "async def compute_manifest(manifest, fe_inputs):\n"
                "    return manifest\n"
            )
        },
    )
    findings = _findings(root)
    assert [(f, ln) for f, ln, _ in findings] == [("app/extract/core.py", 1)]
    assert "static manifests only" in findings[0][2]


def test_a_repo_without_app_or_handler_is_silent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    assert _findings(tmp_path) == []
    (tmp_path / "app").mkdir()
    (tmp_path / "app/connector.py").write_text("class C: ...\n")
    assert _findings(tmp_path) == []


def test_generated_and_test_handlers_do_not_count(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    for rel in ("app/generated/stub.py", "app/tests/test_h.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_HANDLER)
    assert _findings(tmp_path) == []


def test_an_inline_directive_suppresses(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        block="",
        files={
            "app/handler.py": _HANDLER.replace(
                "class MySQLAppHandler(Handler):",
                "# conformance: ignore[P061] served by its own pod until FND-0\n"
                "class MySQLAppHandler(Handler):",
            )
        },
    )
    assert _findings(root) == []
    assert [f.suppressed for f in migration.scan_repo(root)] == [True]


def test_the_runner_entry_point_reports_p061(tmp_path: Path) -> None:
    root = _app(tmp_path, block="")
    paths = sorted(root.rglob("*.py"))
    assert [f.rule_id for f in hosted_handler.scan_all(paths, root)] == ["P061"]


def test_logging_is_left_to_p060(tmp_path: Path) -> None:
    handler = _HANDLER.replace(
        "from .client import SQLClient\n",
        "from .client import SQLClient\nimport logging\nlogging.info('x')\n",
    )
    root = _app(tmp_path, files={"app/handler.py": handler})
    assert _findings(root) == []
    paths = sorted(root.rglob("*.py"))
    assert {f.rule_id for f in hosted_handler.scan_all(paths, root)} == {"P060"}


def test_rule_metadata() -> None:
    rule = get_rule("P061")
    assert rule.name == "HostedHandlerMigration"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.SKILL
    assert rule.remediation_reference.target == migration.SKILL
