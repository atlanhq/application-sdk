"""P053 HostedApiMemberNotThin — the hosted api/ member stays importable on the API server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks import api_member
from conformance.suite.rules import get_rule
from conformance.suite.runner import main as runner_main
from conformance.suite.schema import SarifReport
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_ENTRY_POINT = '\n[project.entry-points."atlan.app_api"]\n{name} = "demo_api:handler"\n'

# Every shape P053 flags, written into the member package.  The not-evaluated
# fixture below reuses it verbatim: the only difference is the entry point.
_VIOLATING_MEMBER = (
    "import os\n"
    "from application_sdk.handler import Handler\n"
    "from app.client import Client\n"
    'TIMEOUT = os.environ.get("TIMEOUT")\n'
)


def _repo(
    root: Path,
    member_source: str,
    *,
    entry_point: str | None = "demo",
    atlan_name: str | None = "demo",
    layout: str = "flat",
) -> Path:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "atlan-demo-app"\nversion = "0.1.0"\n'
    )
    if atlan_name is not None:
        (root / "atlan.yaml").write_text(f"name: {atlan_name}\nentrypoints: []\n")
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "client.py").write_text("import os\nURL = os.getenv('URL')\n")
    api = root / "api"
    pkg = (api / "src" / "demo_api") if layout == "src" else (api / "demo_api")
    pkg.mkdir(parents=True)
    member = '[project]\nname = "demo-api"\nversion = "0.1.0"\n'
    if entry_point is not None:
        member += _ENTRY_POINT.format(name=entry_point)
    (api / "pyproject.toml").write_text(member)
    (pkg / "__init__.py").write_text("from demo_api.handler import handler\n")
    (pkg / "handler.py").write_text(member_source)
    return root


def _p053(root: Path) -> list:
    return [
        f
        for f in api_member.scan_all(api_member.discover(root), root)
        if f.rule_id == "P053"
    ]


def _lines(findings: list, file: str) -> list[int]:
    return sorted(f.line for f in findings if f.file == file)


# ── not evaluated ────────────────────────────────────────────────────────────


@pytest.fixture
def repo_without_entry_point(tmp_path: Path) -> Path:
    """An api/ member carrying every P053 shape, but no atlan.app_api entry point."""
    return _repo(tmp_path, _VIOLATING_MEMBER, entry_point=None, atlan_name="other")


def test_not_evaluated_without_the_entry_point(repo_without_entry_point: Path) -> None:
    assert _p053(repo_without_entry_point) == []


def test_not_evaluated_through_the_runner(repo_without_entry_point: Path) -> None:
    """End to end: no P053 result at all, and P053 cannot fail the run."""
    out = repo_without_entry_point / "out.sarif"
    exit_code = runner_main(
        [
            "--repo",
            str(repo_without_entry_point),
            "--rule",
            "P053",
            "--output",
            str(out),
        ]
    )
    report = SarifReport.model_validate(json.loads(out.read_text()))
    assert [r for r in report.runs[0].results if r.rule_id == "P053"] == []
    assert exit_code == 0


def test_the_same_repo_with_the_entry_point_blocks(tmp_path: Path) -> None:
    """The control for the fixture above: the entry point alone turns it on."""
    root = _repo(tmp_path, _VIOLATING_MEMBER)
    out = root / "out.sarif"
    exit_code = runner_main(
        ["--repo", str(root), "--rule", "P053", "--output", str(out)]
    )
    report = SarifReport.model_validate(json.loads(out.read_text()))
    assert len([r for r in report.runs[0].results if r.rule_id == "P053"]) == 3
    assert exit_code != 0


# ── fires ────────────────────────────────────────────────────────────────────


def test_fires_on_sdk_worker_and_environment(tmp_path: Path) -> None:
    findings = _p053(_repo(tmp_path, _VIOLATING_MEMBER))
    assert _lines(findings, "api/demo_api/handler.py") == [2, 3, 4]
    messages = " ".join(f.message for f in findings)
    assert "application_sdk_api" in messages
    assert "worker package" in messages


@pytest.mark.parametrize(
    "src",
    [
        "import application_sdk\n",
        "import application_sdk.errors as errors\n",
        "from application_sdk.errors import AppError\n",
        "import app\n",
        "from app import client\n",
        "def f():\n    from application_sdk.handler import Handler\n",
        "import os\nX = os.getenv('X')\n",
        "import os as _os\nX = _os.environ['X']\n",
        "from os import environ\nX = environ['X']\n",
        "from os import getenv as ge\nX = ge('X')\n",
        "import os\nclass C:\n    X = os.environ.get('X')\n",
        "import os\ndef f(x=os.getenv('X')):\n    return x\n",
        "import os\nif True:\n    X = os.environ.get('X')\n",
        "import os\ntry:\n    X = os.environ['X']\nexcept KeyError:\n    X = None\n",
    ],
)
def test_fires(tmp_path: Path, src: str) -> None:
    assert len(_p053(_repo(tmp_path, src))) == 1


def test_fires_on_a_src_layout_member(tmp_path: Path) -> None:
    findings = _p053(_repo(tmp_path, "import application_sdk\n", layout="src"))
    assert _lines(findings, "api/src/demo_api/handler.py") == [1]


def test_fires_on_an_entry_point_named_unlike_the_app(tmp_path: Path) -> None:
    (finding,) = _p053(_repo(tmp_path, "", entry_point="demo-server"))
    assert finding.file == "api/pyproject.toml"
    assert finding.line == 6
    assert "'demo'" in finding.message and "atlan.yaml" in finding.message


# ── silent ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "from application_sdk_api.handler import Handler\n",
        "import application_sdk_api.errors as errors\n",
        "from application_sdk_api.handler.contracts import PreflightInput\n",
        "from demo_api.client import Client\n",
        "from .client import Client\n",
        "import os\ndef f():\n    return os.environ.get('X')\n",
        "import os\nclass C:\n    def m(self):\n        return os.getenv('X')\n",
        "import os\nF = lambda: os.getenv('X')\n",
        "import os\nP = os.path.join('a', 'b')\n",
        "import application\nimport apple\n",
    ],
)
def test_silent(tmp_path: Path, src: str) -> None:
    assert _p053(_repo(tmp_path, src)) == []


def test_silent_outside_the_member(tmp_path: Path) -> None:
    """The worker package itself may import the SDK and read the environment."""
    root = _repo(tmp_path, "")
    (root / "app" / "worker.py").write_text("import application_sdk\n")
    assert _p053(root) == []


def test_name_sub_check_skipped_without_a_readable_app_name(tmp_path: Path) -> None:
    assert _p053(_repo(tmp_path, "", entry_point="anything", atlan_name=None)) == []


def test_suppressed_inline(tmp_path: Path) -> None:
    src = "import app  # conformance: ignore[P053] shared constants only, no deps\n"
    (finding,) = _p053(_repo(tmp_path, src))
    assert finding.suppressed


def test_name_sub_check_suppressed_on_the_entry_point_line(tmp_path: Path) -> None:
    root = _repo(tmp_path, "", entry_point="legacy")
    member = root / "api" / "pyproject.toml"
    member.write_text(
        member.read_text().replace(
            'legacy = "demo_api:handler"',
            'legacy = "demo_api:handler"  # conformance: ignore[P053] alias kept one release',
        )
    )
    (finding,) = _p053(root)
    assert finding.suppressed


# ── metadata ─────────────────────────────────────────────────────────────────


def test_rule_metadata() -> None:
    rule = get_rule("P053")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.BLOCK
    assert not rule.autofixable
