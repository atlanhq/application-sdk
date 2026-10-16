"""Tests for P062 TempScratchNotCleanedUp.

The rule fires on ``tempfile.mkdtemp`` / ``mkstemp`` scratch bound to a local
name that is not removed on every exit path.  It is BLOCK and ``/remediate``
applies its fix, so every shape of "already cleaned up" — a ``finally``, a
registered cleanup callback, a broad ``except`` paired with the success path, a
context manager — and every shape of "handed to someone else" must stay silent,
while a happy-path-only removal still fires and names its line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks.worker_disk import scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_HEAD = (
    "import atexit\n"
    "import os\n"
    "import shutil\n"
    "import tempfile\n"
    "from contextlib import ExitStack\n"
    "from pathlib import Path\n"
    "from application_sdk.contracts.types import FileReference\n\n"
)
_OFFSET = _HEAD.count("\n")


def _p062(body: str, *, file: str = "app/x.py") -> list[int]:
    """Lines of the P062 findings in *body*, relative to *body*."""
    findings = scan_text(_HEAD + body, file)
    return [f.line - _OFFSET for f in findings if f.rule_id == "P062"]


def _message(body: str) -> str:
    (finding,) = [f for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "P062"]
    return finding.message


# ── fires ──────────────────────────────────────────────────────────────────────


def test_fires_when_a_mkdtemp_directory_is_never_removed() -> None:
    body = "def f():\n    d = tempfile.mkdtemp(dir='/data')\n    work(d)\n"
    assert _p062(body) == [2]
    message = _message(body)
    assert "never removed" in message
    assert "shutil.rmtree(d, ignore_errors=True)" in message
    assert "TemporaryDirectory(dir=...)" in message


def test_fires_on_a_happy_path_only_removal_and_names_its_line() -> None:
    body = (
        "def f():\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    work(d)\n"
        "    shutil.rmtree(d)\n"
    )
    assert _p062(body) == [2]
    assert f"removed at line {_OFFSET + 4} only when nothing raises" in _message(body)


def test_fires_when_a_mkstemp_file_is_never_removed() -> None:
    body = (
        "def f():\n"
        "    fd, path = tempfile.mkstemp(dir='/data')\n"
        "    os.close(fd)\n"
        "    use(path)\n"
    )
    assert _p062(body) == [2]
    assert "os.remove(path)" in _message(body)


def test_fires_through_a_path_wrapper_and_an_alias_import() -> None:
    body = (
        "from tempfile import mkdtemp as make_scratch\n\n"
        "def f():\n"
        "    d = Path(make_scratch(dir='/data'))\n"
        "    work(d)\n"
    )
    assert _p062(body) == [4]


def test_fires_on_removal_only_in_a_narrow_handler_plus_the_success_path() -> None:
    body = (
        "def f():\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    try:\n"
        "        work(d)\n"
        "    except OSError:\n"
        "        shutil.rmtree(d)\n"
        "        raise\n"
        "    shutil.rmtree(d)\n"
    )
    assert _p062(body) == [2]


def test_a_data_value_computed_from_the_scratch_does_not_hand_it_off() -> None:
    body = (
        "def f():\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    work(d)\n"
        "    return len(os.listdir(d))\n"
    )
    # A count leaves the function, not the directory: nothing else can remove it.
    assert _p062(body) == [2]


# ── silent: removed on every exit ──────────────────────────────────────────────


def test_silent_when_removed_in_a_finally() -> None:
    body = (
        "def f():\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    try:\n"
        "        work(d)\n"
        "    finally:\n"
        "        shutil.rmtree(d, ignore_errors=True)\n"
    )
    assert _p062(body) == []


def test_silent_when_a_mkstemp_file_is_closed_and_removed_in_a_finally() -> None:
    body = (
        "def f():\n"
        "    fd, path = tempfile.mkstemp(dir='/data')\n"
        "    try:\n"
        "        write(fd)\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "        if os.path.exists(path):\n"
        "            os.unlink(path)\n"
    )
    assert _p062(body) == []


def test_silent_when_removed_in_a_broad_except_and_on_success() -> None:
    body = (
        "def f(data):\n"
        "    fd, tmp = tempfile.mkstemp(dir='/data')\n"
        "    try:\n"
        "        write(fd, data)\n"
        "    except Exception:\n"
        "        os.unlink(tmp)\n"
        "        raise\n"
        "    else:\n"
        "        os.unlink(tmp)\n"
    )
    assert _p062(body) == []


@pytest.mark.parametrize(
    "registration",
    [
        "stack.callback(shutil.rmtree, d, ignore_errors=True)",
        "atexit.register(shutil.rmtree, d)",
    ],
)
def test_silent_when_a_cleanup_callback_is_registered(registration: str) -> None:
    body = (
        "def f():\n"
        "    with ExitStack() as stack:\n"
        "        d = tempfile.mkdtemp(dir='/data')\n"
        f"        {registration}\n"
        "        work(d)\n"
    )
    assert _p062(body) == []


def test_silent_when_removed_in_a_finally_through_a_helper() -> None:
    body = (
        "def f():\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    try:\n"
        "        work(d)\n"
        "    finally:\n"
        "        cleanup_scratch(d)\n"
    )
    assert _p062(body) == []


def test_temporary_directory_is_not_graded() -> None:
    body = "def f():\n    with tempfile.TemporaryDirectory(dir='/data') as d:\n        work(d)\n"
    assert _p062(body) == []


# ── silent: handed to someone else ─────────────────────────────────────────────


def test_silent_when_the_scratch_is_returned() -> None:
    assert _p062("def scratch_dir():\n    return tempfile.mkdtemp(dir='/data')\n") == []
    assert (
        _p062("def f():\n    d = tempfile.mkdtemp(dir='/data')\n    return d\n") == []
    )


def test_silent_when_a_file_reference_into_it_is_returned() -> None:
    body = (
        "def f():\n"
        "    out_dir = Path(tempfile.mkdtemp(dir='/data'))\n"
        "    path = out_dir / 'records.jsonl'\n"
        "    write(path)\n"
        "    return FileReference.from_local(str(path))\n"
    )
    assert _p062(body) == []


def test_silent_when_a_path_helper_result_inside_it_is_returned() -> None:
    body = (
        "async def f(item):\n"
        "    retry_dir = tempfile.mkdtemp(dir='/data')\n"
        "    local = await _download_seeds(item, retry_dir)\n"
        "    return local\n"
    )
    assert _p062(body) == []


def test_silent_when_stored_on_self_or_not_bound() -> None:
    body = (
        "class C:\n"
        "    def a(self):\n"
        "        self.d = tempfile.mkdtemp(dir='/data')\n"
        "    def b(self):\n"
        "        work(tempfile.mkdtemp(dir='/data'))\n"
    )
    assert _p062(body) == []


def test_silent_under_a_developer_tooling_directory() -> None:
    body = "def f():\n    d = tempfile.mkdtemp(dir='/data')\n    work(d)\n"
    assert _p062(body, file="scripts/bench.py") == []


def test_silent_when_tempfile_is_not_the_stdlib() -> None:
    src = (
        "from app.scratch import mkdtemp\n\n"
        "def f():\n"
        "    d = mkdtemp()\n"
        "    work(d)\n"
    )
    assert [f for f in scan_text(src, "app/x.py") if f.rule_id == "P062"] == []


# ── suppression, metadata, scope ───────────────────────────────────────────────


def test_suppressed_by_a_justified_directive() -> None:
    body = (
        "def f(cache):\n"
        "    # conformance: ignore[P062] the cache owns it and removes it in close()\n"
        "    d = tempfile.mkdtemp(dir='/data')\n"
        "    cache.register(d)\n"
    )
    findings = [f for f in scan_text(_HEAD + body, "app/x.py") if f.rule_id == "P062"]
    assert [f.suppressed for f in findings] == [True]


def test_rule_metadata() -> None:
    rule = get_rule("P062")
    assert rule.name == "TempScratchNotCleanedUp"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is True
    assert "Customer impact:" in rule.rationale
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.PRESCRIPTION
    assert rule.remediation_reference.target == "programs/areas/prescriptions.prose.md"


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "scratch.py").write_text(
        "import tempfile\n\ndef f():\n    d = tempfile.mkdtemp(dir='/data')\n    work(d)\n",
        encoding="utf-8",
    )
    return tmp_path


def _ids(repo: Path) -> list[str]:
    out = repo / "out.sarif"
    runner.main(["--repo", str(repo), "--rule", "P062", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return [r["ruleId"] for r in doc["runs"][0]["results"]]


def test_runner_reports_p062_on_an_app_repo(tmp_path: Path) -> None:
    assert _ids(_repo(tmp_path, "atlan-demo-app")) == ["P062"]


def test_runner_drops_p062_on_the_sdk(tmp_path: Path) -> None:
    assert _ids(_repo(tmp_path, "atlan-application-sdk")) == []
