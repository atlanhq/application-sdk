"""Tests for .github/scripts/check_symbol_removals.py — the surface-removal gate.

The gate exists because of FND-2388, where nine ``preflight_gate`` names were
deleted without a deprecation cycle and fifteen connector repos hit
``ImportError`` at test collection. So the suite is anchored on that shape:

  * a deleted public name         -> blocking
  * the same name kept as a deprecated alias (``@deprecated`` or a PEP 562
    ``_DEPRECATED_CONSTANTS`` entry) -> no finding at all
  * a deleted underscore-private name -> advisory, never blocking
  * deleting what the BASE already marked deprecated -> no finding (the
    horizon arrived; this is the deletion the deprecation bought)
  * a declared break (``feat!:`` / ``BREAKING CHANGE:``) -> reported, not blocking

Plus the precision rules that keep it usable day to day: an incidental stdlib
import is not surface, an added optional parameter is not narrowing, and a tree
the gate cannot parse exits 2 rather than reporting "clean".
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_symbol_removals as mod
import release as release_mod  # the real parser, to pin the two predicates together

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def snap(source: str, module: str = "application_sdk.thing") -> mod.Snapshot:
    """Build a one-module snapshot from *source*."""
    tree = ast.parse(source)
    snapshot = mod.Snapshot(package="application_sdk")
    for symbol in mod.extract_module(tree, module):
        snapshot.symbols[symbol.key] = symbol
    return snapshot


def keys(findings: list[mod.Finding]) -> set[str]:
    return {f.key for f in findings}


def blocking(findings: list[mod.Finding]) -> set[str]:
    return {f.key for f in findings if f.blocking}


def advisory(findings: list[mod.Finding]) -> set[str]:
    return {f.key for f in findings if not f.blocking}


# ---------------------------------------------------------------------------
# The FND-2388 shape
# ---------------------------------------------------------------------------


def test_deleted_public_function_is_blocking():
    """The `resolve_gate_attempts` case: gone, no alias, nothing said."""
    base = snap("def resolve_gate_attempts(raw): ...")
    head = snap("def gate_attempts(raw): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:resolve_gate_attempts"}


def test_deleted_public_constant_is_blocking():
    """The five removed `CLASSIFICATION_*` / `GATE_RETRY` constants."""
    base = snap("CLASSIFICATION_VERDICT = 'verdict'\nGATE_RETRY = 3\n")
    head = snap("GATE_RETRY = 3\n")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:CLASSIFICATION_VERDICT"}


def test_public_name_in_private_module_is_still_blocking():
    """`_temporal.preflight_gate.resolve_gate_attempts` — private module, public name.

    The module being private does not soften it: the name reads as API to
    anyone who finds it, and nothing in the SDK stopped an app importing it.
    """
    base = snap(
        "def resolve_gate_attempts(raw): ...",
        module="application_sdk.execution._temporal.preflight_gate",
    )
    head = snap("", module="application_sdk.execution._temporal.preflight_gate")
    findings = mod.compare(base, head)
    assert blocking(findings) == {
        "application_sdk.execution._temporal.preflight_gate:resolve_gate_attempts"
    }


def test_deleted_private_name_is_advisory_not_blocking():
    """`_GATE_BROKEN_CATEGORIES` / `_is_gate_broken`.

    Reported so the author sees it, never blocking — renaming a private helper
    is ordinary refactoring, and B008 is the consumer-side fix for the apps
    that imported them.
    """
    base = snap("_GATE_BROKEN_CATEGORIES = frozenset()\ndef _is_gate_broken(e): ...")
    head = snap("")
    findings = mod.compare(base, head)
    assert blocking(findings) == set()
    assert advisory(findings) == {
        "application_sdk.thing:_GATE_BROKEN_CATEGORIES",
        "application_sdk.thing:_is_gate_broken",
    }


def test_public_method_on_a_private_class_is_advisory():
    """The owner is private, so nothing hanging off it was ever offered."""
    base = snap("class _Helper:\n    def run(self, x): ...")
    head = snap("")
    findings = mod.compare(base, head)
    assert blocking(findings) == set()
    assert advisory(findings) == {
        "application_sdk.thing:_Helper",
        "application_sdk.thing:_Helper.run",
    }


def test_dunder_method_is_not_treated_as_private():
    """`__init__` narrowing breaks every subclass, so it is not "private"."""
    base = snap("class A:\n    def __init__(self, a, b): ...")
    head = snap("class A:\n    def __init__(self, a): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:A.__init__"}


# ---------------------------------------------------------------------------
# The escape hatches — and they are the whole point
# ---------------------------------------------------------------------------


def test_deprecated_decorator_alias_clears_the_finding():
    """Keeping the name as a `@deprecated` shim is the supported path."""
    base = snap("def resolve_gate_attempts(raw): ...")
    head = snap(
        "from typing_extensions import deprecated\n"
        "@deprecated('use gate_attempts — removed in v3.40.0')\n"
        "def resolve_gate_attempts(raw): ...\n"
        "def gate_attempts(raw): ...\n"
    )
    assert mod.compare(base, head) == []


def test_qualified_deprecated_decorator_is_recognised():
    """`@typing_extensions.deprecated(...)` is the same marker."""
    base = snap("def old(x): ...")
    head = snap(
        "import typing_extensions\n@typing_extensions.deprecated('x')\ndef old(x): ..."
    )
    assert mod.compare(base, head) == []


def test_pep562_constant_alias_clears_the_finding():
    """A module constant cannot carry `@deprecated`, so the shim is `__getattr__`."""
    base = snap("CLASSIFICATION_VERDICT = 'verdict'")
    head = snap(
        "_DEPRECATED_CONSTANTS = {'CLASSIFICATION_VERDICT': ('X.VERDICT', 'note')}\n"
        "def __getattr__(name):\n"
        "    return _DEPRECATED_CONSTANTS[name]\n"
    )
    assert mod.compare(base, head) == []


def test_alias_mapping_without_getattr_serves_nothing():
    """The dict alone is inert — without the shim the name is really gone."""
    base = snap("CLASSIFICATION_VERDICT = 'verdict'")
    head = snap("_DEPRECATED_CONSTANTS = {'CLASSIFICATION_VERDICT': ('X', 'y')}\n")
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.thing:CLASSIFICATION_VERDICT"
    }


def test_deleting_what_the_base_already_deprecated_is_clean():
    """The deletion the deprecation cycle bought.

    v3.40.0 finally drops the alias: the base shipped it marked, so the gate
    stays quiet. B003 owns whether the horizon had actually arrived.
    """
    base = snap(
        "from typing_extensions import deprecated\n"
        "@deprecated('removed in v3.40.0')\n"
        "def resolve_gate_attempts(raw): ...\n"
    )
    head = snap("")
    assert mod.compare(base, head) == []


@pytest.mark.parametrize(
    "subject",
    [
        "feat!: drop the legacy gate contract",
        "fix(preflight)!: drop the legacy gate contract",
        "refactor!: reshape the gate",
        # A trailer works only when it is IN the title, which is the only part
        # that survives the squash into the commit release.py parses.
        "BREAKING CHANGE: resolve_gate_attempts is gone",
    ],
)
def test_declared_break_downgrades_to_advisory(subject):
    """An intentional break is allowed — it just has to route to a major bump."""
    base = snap("def resolve_gate_attempts(raw): ...")
    head = snap("")
    findings = mod.compare(base, head, commit_subject=subject)
    assert blocking(findings) == set()
    assert advisory(findings) == {"application_sdk.thing:resolve_gate_attempts"}


@pytest.mark.parametrize(
    "subject",
    [
        # Body-only trailer: spec-valid Conventional Commits, and discarded by
        # this repo's squash (title-only subject, blank body), so release.py
        # never sees it and cuts no major.
        "refactor: reshape the gate\n\nBREAKING CHANGE: resolve_gate_attempts is gone",
        # Hyphenated synonym: release.py matches the space form only.
        "refactor: reshape\n\nBREAKING-CHANGE: gone",
        "refactor-CHANGE: not a declaration",
    ],
)
def test_a_declaration_the_release_ignores_does_not_relax_the_gate(subject):
    """The relaxation is only sound if the release automation actually majors.

    Each of these once relaxed the gate while producing no major bump — an
    un-deprecated public removal shipping in a non-major release, which is the
    #3685 shape the gate exists to prevent.
    """
    base = snap("def resolve_gate_attempts(raw): ...")
    head = snap("")
    assert blocking(mod.compare(base, head, commit_subject=subject))


def test_every_accepted_declaration_is_one_release_py_majors():
    """Pin the two predicates together so they cannot drift apart again.

    Asserted against release.py's real parser, not a copy of its regexes.
    """
    accepted = [
        "feat!: drop it",
        "fix(preflight)!: drop it",
        "refactor!: drop it",
        "BREAKING CHANGE: drop it",
    ]
    for subject in accepted:
        assert mod.declares_break(subject), subject
        squashed = mod.squashed_subject(subject)
        is_breaking, _, _ = release_mod.parse_conventional_commits([squashed])
        assert is_breaking, f"gate relaxed on {subject!r} but release.py cuts no major"


@pytest.mark.parametrize(
    "subject",
    ["fix(preflight): enforce the gate by origin", "feat: add a thing", "", None],
)
def test_undeclared_change_stays_blocking(subject):
    """#3685's own subject was a plain `fix(...)`. It must not pass."""
    base = snap("def resolve_gate_attempts(raw): ...")
    head = snap("")
    assert blocking(mod.compare(base, head, commit_subject=subject))


# ---------------------------------------------------------------------------
# Signature narrowing
# ---------------------------------------------------------------------------


def test_removed_parameter_is_narrowing():
    base = snap("def build(app, enforce=True): ...")
    head = snap("def build(app): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:build"}
    assert "enforce" in findings[0].detail


def test_parameter_losing_its_default_is_narrowing():
    base = snap("def build(app, mode='soft'): ...")
    head = snap("def build(app, mode): ...")
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:build"}


def test_added_optional_parameter_is_not_narrowing():
    """Widening is always allowed — that is how the SDK grows."""
    base = snap("def build(app): ...")
    head = snap("def build(app, enforce=True): ...")
    assert mod.compare(base, head) == []


def test_added_keyword_only_with_default_is_not_narrowing():
    base = snap("def build(app): ...")
    head = snap("def build(app, *, enforce=True): ...")
    assert mod.compare(base, head) == []


def test_self_is_not_part_of_a_method_signature():
    """A method reads as the call an app writes, so `self` never counts."""
    base = snap("class A:\n    def go(self, x): ...")
    head = snap("class A:\n    def go(self, x): ...")
    assert mod.compare(base, head) == []


def test_making_a_parameter_keyword_only_is_narrowing():
    """`f(1)` breaks. Silent before review found it."""
    base = snap("def f(x): ...")
    head = snap("def f(*, x): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:f"}
    assert "positional" in findings[0].detail


def test_making_a_parameter_positional_only_is_narrowing():
    """`f(x=1)` breaks. Also silent before."""
    base = snap("def f(x): ...")
    head = snap("def f(x, /): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:f"}
    assert "keyword" in findings[0].detail


def test_positional_only_to_keyword_only_is_narrowing():
    """Swaps which style works rather than removing one — still breaks callers."""
    base = snap("def f(x, /): ...")
    head = snap("def f(*, x): ...")
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:f"}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("def f(*, x): ...", "def f(x): ..."),  # keyword-only -> both
        ("def f(x, /): ...", "def f(x): ..."),  # positional-only -> both
    ],
)
def test_loosening_a_parameter_kind_is_not_narrowing(before, after):
    """Every existing call still works, so there is nothing to report.

    Encoding the kind into the parameter name would report these as a dropped
    `*x` / `/x` — a false positive on a pure widening.
    """
    assert mod.compare(snap(before), snap(after)) == []


def test_vararg_and_keyword_only_of_the_same_name_do_not_collide():
    """They are different kinds and must not compare equal.

    `def f(*items)` accepts `f(1, 2)`; `def f(*, items)` accepts `f(items=1)`.
    A name-encoding scheme renders both `*items` and sees no change.
    """
    base = snap("def f(*items): ...")
    head = snap("def f(*, items): ...")
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:f"}


def test_narrowing_a_deprecated_symbol_is_not_reported():
    """Already marked — B001/B003 own it from here."""
    base = snap(
        "from typing_extensions import deprecated\n"
        "@deprecated('gone in v4')\n"
        "def build(app, enforce=True): ...\n"
    )
    head = snap(
        "from typing_extensions import deprecated\n"
        "@deprecated('gone in v4')\n"
        "def build(app): ...\n"
    )
    assert mod.compare(base, head) == []


# ---------------------------------------------------------------------------
# Re-export precision
# ---------------------------------------------------------------------------


def test_intra_package_reexport_is_surface():
    """`from application_sdk.x import Foo` in an `__init__` publishes `Foo`."""
    base = snap(
        "from application_sdk.app.base import App", module="application_sdk.app"
    )
    head = snap("", module="application_sdk.app")
    assert blocking(mod.compare(base, head)) == {"application_sdk.app:App"}


def test_relative_reexport_is_surface():
    base = snap("from .base import App", module="application_sdk.app")
    head = snap("", module="application_sdk.app")
    assert blocking(mod.compare(base, head)) == {"application_sdk.app:App"}


def test_incidental_stdlib_import_is_not_surface():
    """Tidying an unused `from decimal import Decimal` must not fail the gate.

    This was a real false positive on v3.33.0 -> v3.34.3 before the rule landed.
    """
    base = snap("from decimal import Decimal\ndef f(): ...")
    head = snap("def f(): ...")
    assert mod.compare(base, head) == []


def test_dunder_all_makes_any_import_surface():
    """An explicit `__all__` is the strongest declaration there is."""
    base = snap("from decimal import Decimal\n__all__ = ['Decimal']")
    head = snap("__all__ = []")
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:Decimal"}


def test_dunder_all_excludes_what_it_does_not_list():
    base = snap("from application_sdk.x import A, B\n__all__ = ['A']")
    head = snap("from application_sdk.x import A\n__all__ = ['A']")
    assert mod.compare(base, head) == []


# ---------------------------------------------------------------------------
# Snapshot mechanics
# ---------------------------------------------------------------------------


def test_snapshot_roundtrips_through_json():
    original = snap(
        "from typing_extensions import deprecated\n"
        "@deprecated('x')\n"
        "def old(a, b=1): ...\n"
        "class C:\n    def m(self, q): ...\n"
        "K = 1\n"
    )
    restored = mod.Snapshot.from_json(original.to_json())
    assert restored.symbols == original.symbols


def test_build_snapshot_walks_the_package(tmp_path: Path):
    pkg = tmp_path / "application_sdk" / "sub"
    pkg.mkdir(parents=True)
    (tmp_path / "application_sdk" / "__init__.py").write_text("VERSION = '1'\n")
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("def go(): ...\n")
    snapshot = mod.build_snapshot(tmp_path)
    assert "application_sdk:VERSION" in snapshot.symbols
    assert "application_sdk.sub.mod:go" in snapshot.symbols


def test_unparseable_module_raises_rather_than_skipping(tmp_path: Path):
    """A gate that skips what it cannot read reports a clean tree. Never that."""
    pkg = tmp_path / "application_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("def (:\n")
    with pytest.raises(ValueError, match="cannot parse"):
        mod.build_snapshot(tmp_path)


def test_missing_package_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="no package"):
        mod.build_snapshot(tmp_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_snapshot(path: Path, snapshot: mod.Snapshot) -> None:
    path.write_text(snapshot.to_json(), encoding="utf-8")


def test_cli_compare_exits_1_on_blocking(tmp_path: Path, capsys):
    _write_snapshot(tmp_path / "b.json", snap("def gone(): ..."))
    _write_snapshot(tmp_path / "h.json", snap(""))
    code = mod.main(
        [
            "compare",
            "--base",
            str(tmp_path / "b.json"),
            "--head",
            str(tmp_path / "h.json"),
        ]
    )
    assert code == 1
    assert "BLOCKING" in capsys.readouterr().out


def test_cli_compare_exits_0_when_only_advisory(tmp_path: Path, capsys):
    _write_snapshot(tmp_path / "b.json", snap("def _gone(): ..."))
    _write_snapshot(tmp_path / "h.json", snap(""))
    code = mod.main(
        [
            "compare",
            "--base",
            str(tmp_path / "b.json"),
            "--head",
            str(tmp_path / "h.json"),
        ]
    )
    assert code == 0
    assert "advisory" in capsys.readouterr().out


def test_cli_compare_writes_the_summary_file(tmp_path: Path):
    _write_snapshot(tmp_path / "b.json", snap("def gone(): ..."))
    _write_snapshot(tmp_path / "h.json", snap(""))
    summary = tmp_path / "summary.md"
    mod.main(
        [
            "compare",
            "--base",
            str(tmp_path / "b.json"),
            "--head",
            str(tmp_path / "h.json"),
            "--summary-file",
            str(summary),
        ]
    )
    assert "docs/standards/symbols.md" in summary.read_text()


def test_cli_exits_2_when_it_cannot_run(tmp_path: Path):
    """Infrastructure failure is not success. Distinct code, distinct meaning."""
    (tmp_path / "bad.json").write_text("{not json")
    _write_snapshot(tmp_path / "h.json", snap(""))
    code = mod.main(
        [
            "compare",
            "--base",
            str(tmp_path / "bad.json"),
            "--head",
            str(tmp_path / "h.json"),
        ]
    )
    assert code == 2


def test_cli_snapshot_writes_json(tmp_path: Path):
    pkg = tmp_path / "application_sdk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def go(): ...\n")
    out = tmp_path / "snap.json"
    assert mod.main(["snapshot", "--root", str(tmp_path), "-o", str(out)]) == 0
    assert "application_sdk:go" in json.loads(out.read_text())["symbols"]


def test_clean_report_says_so():
    report = mod.render_report([], base_label="v3.35.0", head_label="HEAD")
    assert "No names were removed" in report


# ---------------------------------------------------------------------------
# `check` — the CI entry point, against a real git repo
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    """A throwaway repo with one released tag carrying a public symbol."""
    repo = tmp_path / "repo"
    (repo / "application_sdk").mkdir(parents=True)
    _run_git(repo.parent, "init", "-q", "repo")
    _run_git(repo, "config", "user.email", "t@example.com")
    _run_git(repo, "config", "user.name", "t")
    (repo / "application_sdk" / "__init__.py").write_text(
        "def resolve_gate_attempts(raw): ...\ndef _helper(): ...\n"
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-qm", "release")
    _run_git(repo, "tag", "v3.35.0")
    return repo


def test_check_flags_a_removal_against_the_tag(tagged_repo: Path, capsys):
    (tagged_repo / "application_sdk" / "__init__.py").write_text(
        "def gate_attempts(raw): ...\n"
    )
    code = mod.main(["check", "--repo", str(tagged_repo)])
    assert code == 1
    out = capsys.readouterr().out
    assert "v3.35.0" in out
    assert "resolve_gate_attempts" in out


def test_check_is_clean_when_the_name_is_aliased(tagged_repo: Path):
    (tagged_repo / "application_sdk" / "__init__.py").write_text(
        "from typing_extensions import deprecated\n"
        "@deprecated('use gate_attempts — removed in v3.40.0')\n"
        "def resolve_gate_attempts(raw): ...\n"
        "def gate_attempts(raw): ...\n"
        "def _helper(): ...\n"
    )
    assert mod.main(["check", "--repo", str(tagged_repo)]) == 0


def test_check_reads_the_working_tree_not_the_commit(tagged_repo: Path):
    """Uncommitted removals count — the gate runs before anything is merged."""
    (tagged_repo / "application_sdk" / "__init__.py").write_text("")
    assert mod.main(["check", "--repo", str(tagged_repo)]) == 1


def test_latest_release_tag_skips_prereleases(tagged_repo: Path):
    _run_git(tagged_repo, "tag", "v3.36.0-rc1")
    assert mod.latest_release_tag(tagged_repo) == "v3.35.0"


def test_latest_release_tag_picks_the_newest_by_version(tagged_repo: Path):
    _run_git(tagged_repo, "tag", "v3.9.0")
    _run_git(tagged_repo, "tag", "v3.36.0")
    assert mod.latest_release_tag(tagged_repo) == "v3.36.0"


def test_check_exits_2_when_no_tag_is_reachable(tmp_path: Path):
    """A shallow checkout must not read as a clean surface."""
    repo = tmp_path / "untagged"
    (repo / "application_sdk").mkdir(parents=True)
    _run_git(repo.parent, "init", "-q", "untagged")
    _run_git(repo, "config", "user.email", "t@example.com")
    _run_git(repo, "config", "user.name", "t")
    (repo / "application_sdk" / "__init__.py").write_text("def f(): ...\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-qm", "c")
    assert mod.main(["check", "--repo", str(repo)]) == 2
