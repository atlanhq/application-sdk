"""P054 HostedHandlerLogs — hosted handler code does not log."""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks import hosted_handler
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_HOSTED = '[project]\nname = "atlan-demo-app"\n\n[tool.atlan-app-api]\nhandler = "app.handler:DemoHandler"\n'


def _app(tmp_path: Path, files: dict[str, str], pyproject: str = _HOSTED) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _ids(root: Path) -> list[tuple[str, int]]:
    paths = sorted(root.rglob("*.py"))
    return [
        (f.file, f.line)
        for f in hosted_handler.scan_all(paths, root)
        if not f.suppressed
    ]


def test_logging_in_the_handler_and_its_imports_fires(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        {
            "app/handler.py": (
                "from application_sdk.observability.logger_adaptor import get_logger\n"
                "from .client import C\n"
                "logger = get_logger(__name__)\n"
                "def f():\n"
                "    logger.info('x')\n"
            ),
            "app/client.py": "import logging\nlog = logging.getLogger(__name__)\n",
            "app/worker.py": "import logging\nlogging.info('worker logs are fine')\n",
        },
    )
    assert _ids(root) == [
        ("app/client.py", 1),
        ("app/client.py", 2),
        ("app/handler.py", 1),
        ("app/handler.py", 3),
        ("app/handler.py", 5),
    ]


def test_context_log_methods_fire(tmp_path: Path) -> None:
    root = _app(
        tmp_path, {"app/handler.py": "def f(self):\n    self.context.log_info('x')\n"}
    )
    assert _ids(root) == [("app/handler.py", 2)]


@pytest.mark.parametrize(
    "src",
    [
        "from .client import C\nfrom application_sdk.errors import AuthError\n",
        "def f():\n    raise ValueError('not a log')\n",
        "items = []\nitems.append(1)\n",
    ],
)
def test_handler_code_without_logging_is_silent(tmp_path: Path, src: str) -> None:
    root = _app(tmp_path, {"app/handler.py": src, "app/client.py": ""})
    assert _ids(root) == []


def test_an_app_that_is_not_hosted_is_silent(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        {"app/handler.py": "import logging\nlogging.info('x')\n"},
        pyproject='[project]\nname = "atlan-demo-app"\n',
    )
    assert _ids(root) == []


def test_suppression(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        {
            "app/handler.py": "import logging  # conformance: ignore[P054] vendored shim\n"
        },
    )
    assert _ids(root) == []


def test_rule_metadata() -> None:
    rule = get_rule("P054")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.BLOCK
    assert not rule.autofixable
