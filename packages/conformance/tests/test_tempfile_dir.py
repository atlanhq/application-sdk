"""Tests for O008 TempFileOutsideWorkerVolume.

The rule fires on stdlib ``tempfile`` constructors that fall back to the system
temp dir — ``dir=`` omitted, ``dir=None``, or ``dir=tempfile.gettempdir()`` —
and on nothing else: an explicit directory (literal or computed), a splat that
may supply one, and any non-stdlib callable that happens to share a name all
stay silent.  The positional ``dir`` slot differs per constructor, so the
position table is re-derived from the running interpreter rather than trusted.
"""

from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks.optimizations import scan_text
from conformance.suite.checks.optimizations._tempfile_dir import (
    DIR_POSITION,
    collect_tempfile_bindings,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _o008(src: str) -> list[int]:
    """Lines of the O008 findings in *src*."""
    return [f.line for f in scan_text(src, "app/x.py") if f.rule_id == "O008"]


def _call(name: str, args: str) -> str:
    return f"import tempfile\n\ndef f(base, opts, rest):\n    tempfile.{name}({args})\n"


# ── the positional table matches the interpreter ───────────────────────────────


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_dir_position_matches_the_stdlib_signature(name: str) -> None:
    params = list(inspect.signature(getattr(tempfile, name)).parameters)
    assert params.index("dir") == DIR_POSITION[name]


# ── fires ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_fires_when_dir_is_omitted(name: str) -> None:
    assert _o008(_call(name, "")) == [4]


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_fires_on_dir_none_keyword(name: str) -> None:
    assert _o008(_call(name, "dir=None")) == [4]


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_fires_on_dir_none_in_the_positional_slot(name: str) -> None:
    args = ", ".join(["None"] * (DIR_POSITION[name] + 1))
    assert _o008(_call(name, args)) == [4]


def test_fires_when_positionals_stop_short_of_the_dir_slot() -> None:
    # NamedTemporaryFile's dir is 7th; two positionals leave it at its default.
    assert _o008(_call("NamedTemporaryFile", "'w', -1")) == [4]


def test_fires_on_dir_gettempdir_in_both_import_forms() -> None:
    src = (
        "import tempfile\n"
        "from tempfile import gettempdir\n\n"
        "def f():\n"
        "    tempfile.mkdtemp(dir=tempfile.gettempdir())\n"
        "    tempfile.mkstemp(dir=gettempdir())\n"
    )
    assert _o008(src) == [5, 6]


def test_fires_through_module_and_function_aliases() -> None:
    src = (
        "import tempfile as tf\n"
        "from tempfile import NamedTemporaryFile as NTF, mkdtemp\n\n"
        "def f():\n"
        "    tf.TemporaryDirectory(prefix='x-')\n"
        "    NTF('w', suffix='.json', delete=False)\n"
        "    mkdtemp()\n"
    )
    assert _o008(src) == [5, 6, 7]


def test_fires_on_a_function_local_import() -> None:
    src = "def f():\n    import tempfile\n    return tempfile.mkstemp(suffix='.part')\n"
    assert _o008(src) == [3]


def test_message_names_the_constructor_the_cause_and_the_fix() -> None:
    (finding,) = [
        f
        for f in scan_text(_call("mkdtemp", "prefix='x-'"), "app/x.py")
        if f.rule_id == "O008"
    ]
    assert finding.message.startswith("tempfile.mkdtemp() with no dir= ")
    for needle in ("$TMPDIR, else /tmp", "TEMPORARY_PATH", "node's root disk"):
        assert needle in finding.message


# ── silent: compliant or unknowable ────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_silent_on_an_explicit_dir_keyword(name: str) -> None:
    assert _o008(_call(name, "dir=base")) == []


@pytest.mark.parametrize(
    "value", ["'/app/local/tmp'", "str(base)", "os.path.join(base, 'x')", "base.dir"]
)
def test_silent_on_any_other_dir_expression(value: str) -> None:
    assert _o008(_call("mkdtemp", f"dir={value}")) == []


@pytest.mark.parametrize("name", sorted(DIR_POSITION))
def test_silent_on_a_directory_in_the_positional_slot(name: str) -> None:
    args = ", ".join(["None"] * DIR_POSITION[name] + ["base"])
    assert _o008(_call(name, args)) == []


def test_silent_when_a_splat_may_supply_dir() -> None:
    src = (
        "import tempfile\n\n"
        "def f(opts, rest):\n"
        "    tempfile.mkdtemp(**opts)\n"
        "    tempfile.mkdtemp(*rest)\n"
        "    tempfile.NamedTemporaryFile('w', *rest)\n"
    )
    assert _o008(src) == []


def test_silent_on_a_starred_argument_after_the_dir_slot() -> None:
    # The slot is filled positionally before the splat, so the splat cannot move it.
    assert _o008(_call("mkdtemp", "'s', 'p', base, *rest")) == []


# ── silent: binding negatives and out-of-scope calls ───────────────────────────


def test_silent_on_an_app_defined_helper_of_the_same_name() -> None:
    src = "def mkdtemp(prefix=''):\n    return prefix\n\ndef f():\n    mkdtemp()\n"
    assert _o008(src) == []


def test_silent_when_the_name_comes_from_another_module() -> None:
    src = (
        "from app.scratch import mkdtemp, NamedTemporaryFile\n"
        "import app.scratch as tempfile\n\n"
        "def f(obj):\n"
        "    mkdtemp()\n"
        "    NamedTemporaryFile()\n"
        "    obj.mkdtemp()\n"
    )
    # ``import app.scratch as tempfile`` binds the name to an app module, not the stdlib.
    assert _o008(src) == []


def test_silent_on_constructors_outside_the_graded_set() -> None:
    src = (
        "import tempfile\n\n"
        "def f():\n"
        "    tempfile.mktemp()\n"
        "    tempfile.gettempdir()\n"
        "    tempfile.gettempprefix()\n"
    )
    assert _o008(src) == []


def test_collect_bindings_reports_modules_and_functions() -> None:
    import ast

    tree = ast.parse(
        "import tempfile as tf\n"
        "from tempfile import mkdtemp as mk, gettempdir, mktemp\n"
    )
    module_names, func_names = collect_tempfile_bindings(tree)
    assert module_names == frozenset({"tf"})
    assert func_names == {"mk": "mkdtemp", "gettempdir": "gettempdir"}


# ── suppression, metadata, scope ───────────────────────────────────────────────


def test_suppressed_by_a_justified_directive() -> None:
    src = (
        "import tempfile\n\n"
        "def f():\n"
        "    return tempfile.mkdtemp()  # conformance: ignore[O008] TMPDIR points at the volume\n"
    )
    findings = [f for f in scan_text(src, "app/x.py") if f.rule_id == "O008"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_rule_metadata() -> None:
    rule = get_rule("O008")
    assert rule.name == "TempFileOutsideWorkerVolume"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is True
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.PRESCRIPTION
    assert rule.remediation_reference.target == "programs/areas/optimizations.prose.md"


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "scratch.py").write_text(
        "import tempfile\n\ndef make():\n    return tempfile.mkdtemp()\n",
        encoding="utf-8",
    )
    # Tests are excluded by the standard discovery walk.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_scratch.py").write_text(
        "import tempfile\n\ndef test_x():\n    assert tempfile.mkdtemp()\n",
        encoding="utf-8",
    )
    return tmp_path


def _results(repo: Path) -> list[tuple[str, str]]:
    out = repo / "out.sarif"
    runner.main(["--repo", str(repo), "--rule", "O008", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return [
        (r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"])
        for r in doc["runs"][0]["results"]
    ]


def test_runner_reports_app_code_and_skips_tests(tmp_path: Path) -> None:
    assert _results(_repo(tmp_path, "atlan-demo-app")) == [("O008", "app/scratch.py")]


def test_runner_drops_o008_on_the_sdk(tmp_path: Path) -> None:
    assert _results(_repo(tmp_path, "atlan-application-sdk")) == []
