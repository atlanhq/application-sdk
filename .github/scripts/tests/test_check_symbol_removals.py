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
from pathlib import Path

import check_symbol_removals as mod
import pytest
import release as release_mod  # the real parser, to pin the two predicates together

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def snap(source: str, module: str = "application_sdk.thing") -> mod.Snapshot:
    """Build a one-module snapshot from *source*."""
    tree = ast.parse(source)
    snapshot = mod.Snapshot(package="application_sdk")
    mod.record_module(snapshot, tree, module)
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


def test_factory_built_getattr_alias_clears_the_finding():
    """`__getattr__ = deprecated_module_getattr(...)` is the shared shim (FND-4300)."""
    base = snap("CLASSIFICATION_VERDICT = 'verdict'")
    head = snap(
        "from application_sdk import _deprecation\n"
        "_DEPRECATED_CONSTANTS = {'CLASSIFICATION_VERDICT': ('X.VERDICT', 'note')}\n"
        "__getattr__ = _deprecation.deprecated_module_getattr(\n"
        "    __name__, _DEPRECATED_CONSTANTS, remove_in='4.0.0'\n"
        ")\n"
    )
    assert mod.compare(base, head) == []


def test_an_unrelated_callable_assigned_to_getattr_serves_no_alias():
    """Only the shared factory is known to serve its table."""
    base = snap("OLD = 1")
    head = snap(
        "_DEPRECATED_CONSTANTS = {'OLD': ('NEW', 'moved')}\n"
        "__getattr__ = make_getter()\n"
    )
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:OLD"}


def test_factory_fallback_helper_serves_its_allowlist():
    """A factory getter handed `fallback=_lazy` serves what `_lazy` reads."""
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "_SERVICE_NAMES = frozenset({'create_service'})\n"
        "_DEPRECATED_CONSTANTS = {'OLD': ('X', 'y')}\n"
        "def _lazy(name):\n"
        "    if name in _SERVICE_NAMES:\n"
        "        from application_sdk.thing import service\n"
        "        return getattr(service, name)\n"
        "    raise AttributeError(name)\n"
        "__getattr__ = _deprecation.deprecated_module_getattr(\n"
        "    __name__, _DEPRECATED_CONSTANTS, remove_in='4.0.0', fallback=_lazy\n"
        ")\n"
        "__all__ = ['create_service']\n"
    )
    assert mod.compare(base, head) == []


def test_alias_mapping_without_getattr_serves_nothing():
    """The dict alone is inert — without the shim the name is really gone."""
    base = snap("CLASSIFICATION_VERDICT = 'verdict'")
    head = snap("_DEPRECATED_CONSTANTS = {'CLASSIFICATION_VERDICT': ('X', 'y')}\n")
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.thing:CLASSIFICATION_VERDICT"
    }


def test_a_reexport_inside_a_top_level_with_block_is_surface():
    """`preflight_gate` re-exports through `imports_passed_through()`.

    A `with` block opens no scope, so the name is as importable as one bound
    beside it. Moving the definition elsewhere and re-exporting it from inside
    the block must not read as a removal.
    """
    base = snap("EMPTY_CHECK_MATRIX = '[]'")
    head = snap(
        "with workflow.unsafe.imports_passed_through():\n"
        "    from application_sdk.handler._outcome import (\n"
        "        EMPTY_CHECK_MATRIX as EMPTY_CHECK_MATRIX,\n"
        "    )\n"
    )
    assert mod.compare(base, head) == []


def test_dropping_a_reexport_from_a_with_block_is_a_removal():
    """The other direction: the block is read on the base side too."""
    base = snap(
        "with workflow.unsafe.imports_passed_through():\n"
        "    from application_sdk.errors.leaves import PreconditionError\n"
    )
    head = snap("")
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.thing:PreconditionError"
    }


def test_type_checking_binding_alone_is_not_surface():
    """`if TYPE_CHECKING:` binds nothing at runtime, so it serves nothing."""
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "__all__ = ['create_service']\n"
    )
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:create_service"}


def test_pep562_lazy_reexport_is_surface():
    """`handler/__init__` serves the HTTP service names lazily (FND-3280).

    The `TYPE_CHECKING` import, a module `__getattr__` and an `__all__` entry
    together declare an importable name, so it is still present.
    """
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "_SERVICE_NAMES = frozenset({'create_service'})\n"
        "def __getattr__(name):\n"
        "    if name in _SERVICE_NAMES:\n"
        "        from application_sdk.thing import service\n"
        "        return getattr(service, name)\n"
        "    raise AttributeError(name)\n"
        "__all__ = ['create_service']\n"
    )
    assert mod.compare(base, head) == []


def test_a_getter_that_never_names_the_export_is_a_removal():
    """Declared in `__all__` and `TYPE_CHECKING`, but the getter dropped it.

    `from ... import create_service` fails at runtime, so the name is gone even
    though the type-only import and the `__all__` entry survived.
    """
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "_SERVICE_NAMES = frozenset({'run_service'})\n"
        "def __getattr__(name):\n"
        "    if name in _SERVICE_NAMES:\n"
        "        return object()\n"
        "    raise AttributeError(name)\n"
        "__all__ = ['create_service', 'run_service']\n"
    )
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:create_service"}


def test_a_getter_that_names_the_export_only_to_reject_it_is_a_removal():
    """A literal in the getter body is not evidence it serves the name.

    `raise AttributeError("create_service was removed")` mentions the name and
    serves nothing. Only an allowlist the getter reads counts.
    """
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "def __getattr__(name):\n"
        "    if name == 'create_service':\n"
        "        raise AttributeError('create_service was removed')\n"
        "    raise AttributeError(name)\n"
        "__all__ = ['create_service']\n"
    )
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:create_service"}


def test_lazy_name_missing_from_all_is_still_a_removal():
    """Without the `__all__` declaration the gate cannot tell it is served."""
    base = snap("from application_sdk.thing.service import create_service")
    head = snap(
        "if TYPE_CHECKING:\n"
        "    from application_sdk.thing.service import create_service\n"
        "def __getattr__(name):\n"
        "    raise AttributeError(name)\n"
        "__all__ = []\n"
    )
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:create_service"}


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


def test_every_symbol_kind_is_recorded_as_the_enum():
    """Each of the six kinds comes out as a ``SymbolKind`` member, and the JSON
    snapshot stores its plain string (a base snapshot from an older script must
    still load)."""
    original = snap(
        "from application_sdk.other import Moved\n"
        "_DEPRECATED_CONSTANTS = {'Old': 'application_sdk.other.New'}\n"
        "def __getattr__(name): ...\n"
        "def f(): ...\n"
        "class C:\n    def m(self): ...\n"
        "K = 1\n"
    )
    kinds = {s.qualname: s.kind for s in original.symbols.values()}
    assert all(type(k) is mod.SymbolKind for k in kinds.values())
    assert kinds["f"] is mod.SymbolKind.FUNCTION
    assert kinds["C"] is mod.SymbolKind.CLASS
    assert kinds["C.m"] is mod.SymbolKind.METHOD
    assert kinds["K"] is mod.SymbolKind.CONSTANT
    assert kinds["Moved"] is mod.SymbolKind.REEXPORT
    assert kinds["Old"] is mod.SymbolKind.ALIAS

    raw = json.loads(original.to_json())
    assert {rec["kind"] for rec in raw["symbols"].values()} == {
        "function",
        "class",
        "method",
        "constant",
        "reexport",
        "alias",
    }
    restored = mod.Snapshot.from_json(original.to_json())
    assert all(type(s.kind) is mod.SymbolKind for s in restored.symbols.values())


def test_a_snapshot_with_an_unknown_kind_does_not_load():
    """A seventh kind the gate does not know is a gate that cannot see: refuse
    it rather than compare a string no branch handles."""
    text = (
        '{"package": "application_sdk", "symbols": {"m:x": {"module": "m",'
        ' "qualname": "x", "kind": "property", "deprecated": false}}}'
    )
    with pytest.raises(ValueError, match="property"):
        mod.Snapshot.from_json(text)


def test_snapshot_roundtrips_its_all_facts_through_json():
    original = snap(
        "from application_sdk.other import Moved, Kept\n__all__ = ['Kept', 'f']\n"
        "def f(): ...\n"
    )
    assert original.declared_modules == {"application_sdk.thing"}
    assert original.undeclared == {"application_sdk.thing:Moved"}
    restored = mod.Snapshot.from_json(original.to_json())
    assert restored.declared_modules == original.declared_modules
    assert restored.undeclared == original.undeclared


def test_a_snapshot_from_before_the_all_facts_still_loads():
    """A base snapshot written before these fields existed did not record which
    modules declared ``__all__``: that is unknown, not "none did"."""
    text = '{"package": "application_sdk", "symbols": {}}'
    restored = mod.Snapshot.from_json(text)
    assert restored.declared_modules is None
    assert restored.undeclared == set()


def test_a_legacy_base_cannot_waive_narrowing_an_existing_all():
    """The base module already declared ``__all__`` with the import listed, but
    an old-format snapshot did not record that. Reading the missing field as
    "no module declared ``__all__``" would turn this narrowing advisory."""
    full = snap(_LEAF_BASE + "__all__ = ['handler', 'DEPLOYMENT_NAME']\n")
    raw = json.loads(full.to_json())
    del raw["declared_modules"], raw["undeclared"]
    base = mod.Snapshot.from_json(json.dumps(raw))
    head = snap(_LEAF_BASE + "__all__ = ['handler']\n")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:DEPLOYMENT_NAME"}


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


# ---------------------------------------------------------------------------
# Lazy package exports and re-export resolution (the api-distribution split)
# ---------------------------------------------------------------------------


def _tree_snapshot(tmp_path: Path, files: dict[str, str]) -> mod.Snapshot:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return mod.build_snapshot(tmp_path)


_LAZY_INIT = """
import importlib
from typing import Any

_LAZY: dict[str, tuple[str, str]] = {
    "Thing": ("application_sdk.pkg.thing", "Thing"),
}


def __getattr__(name: str) -> Any:
    module, attr = _LAZY[name]
    return getattr(importlib.import_module(module), attr)
"""

_THING = "class Thing:\n    def run(self, x): ...\n"


def test_moving_a_reexport_behind_a_lazy_map_is_not_a_removal(tmp_path: Path):
    base = _tree_snapshot(
        tmp_path / "base",
        {
            "application_sdk/pkg/__init__.py": "from application_sdk.pkg.thing import Thing\n",
            "application_sdk/pkg/thing.py": _THING,
        },
    )
    head = _tree_snapshot(
        tmp_path / "head",
        {
            "application_sdk/pkg/__init__.py": _LAZY_INIT,
            "application_sdk/pkg/thing.py": _THING,
        },
    )
    assert "application_sdk.pkg:Thing.run" in base.symbols
    assert mod.compare(base, head) == []


def test_a_lazy_map_without_getattr_serves_nothing(tmp_path: Path):
    base = _tree_snapshot(
        tmp_path / "base",
        {
            "application_sdk/pkg/__init__.py": "from application_sdk.pkg.thing import Thing\n",
            "application_sdk/pkg/thing.py": _THING,
        },
    )
    head = _tree_snapshot(
        tmp_path / "head",
        {
            "application_sdk/pkg/__init__.py": _LAZY_INIT.split("def __getattr__")[0],
            "application_sdk/pkg/thing.py": _THING,
        },
    )
    assert "application_sdk.pkg:Thing" in blocking(mod.compare(base, head))


def test_a_class_moved_and_reexported_keeps_its_methods(tmp_path: Path):
    base = _tree_snapshot(
        tmp_path / "base",
        {"application_sdk/__init__.py": "", "application_sdk/old.py": _THING},
    )
    head = _tree_snapshot(
        tmp_path / "head",
        {
            "application_sdk/__init__.py": "",
            "application_sdk/new.py": _THING,
            "application_sdk/old.py": "from application_sdk.new import Thing\n",
        },
    )
    assert mod.compare(base, head) == []


def test_narrowing_a_moved_class_method_is_still_caught(tmp_path: Path):
    base = _tree_snapshot(
        tmp_path / "base",
        {"application_sdk/__init__.py": "", "application_sdk/old.py": _THING},
    )
    head = _tree_snapshot(
        tmp_path / "head",
        {
            "application_sdk/__init__.py": "",
            "application_sdk/new.py": "class Thing:\n    def run(self): ...\n",
            "application_sdk/old.py": "from application_sdk.new import Thing\n",
        },
    )
    assert "application_sdk.old:Thing.run" in blocking(mod.compare(base, head))


# ---------------------------------------------------------------------------
# Deprecated parameters (FND-5542)
#
# A parameter cannot carry @deprecated; docs/standards/symbols.md deprecates it
# with a DeprecationWarning raised when a caller passes it. Deleting a parameter
# the base release deprecated that way is the removal the deprecation bought.
# ---------------------------------------------------------------------------

#: The base-release shape of ``create_app_handler_service(state_store=)``, the
#: removal that FND-4094's #5178 was blocked on.
_STATE_STORE_BASE = """
import warnings

def create_app_handler_service(handler, *, title="x", state_store=None):
    \"\"\"Create the app.\"\"\"
    global _x
    if state_store is not None:
        warnings.warn(
            "state_store parameter is deprecated and ignored. "
            "Use DaprCredentialVault. Will be removed in v3.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
    return handler
"""

_STATE_STORE_HEAD = """
def create_app_handler_service(handler, *, title="x"):
    return handler
"""


def test_removing_a_parameter_the_base_deprecated_is_clean():
    base = snap(_STATE_STORE_BASE)
    assert base.symbols[
        "application_sdk.thing:create_app_handler_service"
    ].deprecated_params == ("state_store",)
    assert mod.compare(base, snap(_STATE_STORE_HEAD)) == []


def test_removing_a_parameter_the_base_did_not_deprecate_still_blocks():
    """Only the named parameter is excused, never the rest of the signature."""
    base = snap(_STATE_STORE_BASE)
    head = snap("def create_app_handler_service(handler, *, state_store=None): ...")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:create_app_handler_service"}
    assert "title" in findings[0].detail
    assert "state_store" not in findings[0].detail


@pytest.mark.parametrize(
    "notice",
    [
        "_deprecation.warn_deprecated('f(old=...)', 'f(new=...)', '4.0.0')",
        "warn_deprecated(name='f(old=...)', replacement='new', remove_in='4.0.0')",
        "warnings.warn(f'`old` is deprecated: {x}', category=DeprecationWarning)",
        "warnings.warn('old is deprecated', builtins.DeprecationWarning)",
    ],
)
def test_every_notice_shape_marks_the_parameter(notice: str):
    base = snap(
        f"def f(new=None, old=None):\n    if old is not None:\n        {notice}\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ("old",)
    assert mod.compare(base, snap("def f(new=None): ...")) == []


@pytest.mark.parametrize(
    "guard",
    ["old is not None", "old is not _UNSET", "old"],
)
def test_every_presence_check_marks_the_parameter(guard: str):
    base = snap(
        f"def f(old=_UNSET):\n    if {guard}:\n"
        "        warn_deprecated('f(old=...)', 'nothing', '4.0.0')\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ("old",)


def test_a_notice_under_is_none_deprecates_the_default_not_the_parameter():
    """The symbols.md example: warning when ``raise_on_error`` is *omitted*.

    Its notice names the parameter, but deleting it would remove the very
    thing callers were told to start passing.
    """
    base = snap(
        "def get(key, raise_on_error=None):\n"
        "    if raise_on_error is None:\n"
        "        _deprecation.warn_deprecated(\n"
        "            'get() without raise_on_error',\n"
        "            'raise_on_error=True for a typed error',\n"
        "            '4.0.0',\n"
        "        )\n"
    )
    assert base.symbols["application_sdk.thing:get"].deprecated_params == ()
    assert blocking(mod.compare(base, snap("def get(key): ..."))) == {
        "application_sdk.thing:get"
    }


def test_a_notice_about_one_form_of_the_value_keeps_the_parameter():
    """``resolve_credential_file(dest_dir=<str>)``: only a plain string is deprecated."""
    base = snap(
        "def resolve(dest_dir=None):\n"
        "    if dest_dir is not None and not isinstance(dest_dir, Dir):\n"
        "        warnings.warn('a plain-string dest_dir is deprecated', DeprecationWarning)\n"
    )
    assert base.symbols["application_sdk.thing:resolve"].deprecated_params == ()


def test_a_notice_that_does_not_name_the_guarded_parameter_marks_nothing():
    base = snap(
        "def f(strict=False):\n"
        "    if strict:\n"
        "        warnings.warn('lenient mode is deprecated', DeprecationWarning)\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ()


def test_the_replacement_named_in_warn_deprecated_is_not_marked():
    """Only ``name`` says what is deprecated; ``replacement`` names what to use."""
    base = snap(
        "def f(new=None, old=None):\n"
        "    if old is not None:\n"
        "        if new is not None:\n"
        "            warn_deprecated('f(old=...)', 'f(new=...)', '4.0.0')\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ("old",)


def test_a_whole_word_match_is_required():
    base = snap(
        "def f(store=None):\n"
        "    if store is not None:\n"
        "        warnings.warn('state_store is deprecated', DeprecationWarning)\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ()


def test_a_user_warning_does_not_mark_the_parameter():
    base = snap(
        "def f(old=None):\n"
        "    if old is not None:\n"
        "        warnings.warn('old is ignored', UserWarning)\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ()


def test_a_notice_in_a_nested_def_belongs_to_it():
    base = snap(
        "def f(old=None):\n"
        "    if old is not None:\n"
        "        def inner():\n"
        "            warnings.warn('old is deprecated', DeprecationWarning)\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ()


def test_a_notice_in_the_else_branch_is_not_guarded_by_the_presence_check():
    base = snap(
        "def f(old=None):\n"
        "    if old is not None:\n"
        "        pass\n"
        "    else:\n"
        "        warnings.warn('omitting old is deprecated', DeprecationWarning)\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ()


def test_a_notice_nested_under_other_blocks_still_counts():
    base = snap(
        "def f(old=None):\n"
        "    try:\n"
        "        if old is not None:\n"
        "            for _ in range(1):\n"
        "                warnings.warn('old is deprecated', DeprecationWarning)\n"
        "    finally:\n"
        "        pass\n"
    )
    assert base.symbols["application_sdk.thing:f"].deprecated_params == ("old",)


def test_a_deprecated_method_parameter_may_be_removed():
    base = snap(
        "class C:\n"
        "    def run(self, old=None):\n"
        "        if old is not None:\n"
        "            warn_deprecated('C.run(old=...)', 'nothing', '4.0.0')\n"
    )
    head = snap("class C:\n    def run(self): ...\n")
    assert mod.compare(base, head) == []


def test_a_deprecated_parameter_hardened_rather_than_removed_still_blocks():
    """Only deletion is excused: until then, existing calls must keep working."""
    base = snap(
        "def f(old=None):\n"
        "    if old is not None:\n"
        "        warn_deprecated('f(old=...)', 'nothing', '4.0.0')\n"
    )
    head = snap("def f(*, old=None): ...")
    assert blocking(mod.compare(base, head)) == {"application_sdk.thing:f"}


def test_a_reexport_inherits_the_deprecated_parameters(tmp_path: Path):
    """#5178 failed twice: at the defining module and at the package re-export."""
    init = "from application_sdk.handler.service import create_app_handler_service\n"
    base = _tree_snapshot(
        tmp_path / "base",
        {
            "application_sdk/__init__.py": "",
            "application_sdk/handler/__init__.py": init,
            "application_sdk/handler/service.py": _STATE_STORE_BASE,
        },
    )
    head = _tree_snapshot(
        tmp_path / "head",
        {
            "application_sdk/__init__.py": "",
            "application_sdk/handler/__init__.py": init,
            "application_sdk/handler/service.py": _STATE_STORE_HEAD,
        },
    )
    assert base.symbols[
        "application_sdk.handler:create_app_handler_service"
    ].deprecated_params == ("state_store",)
    assert mod.compare(base, head) == []


def test_deprecated_parameters_roundtrip_through_json():
    original = snap(_STATE_STORE_BASE)
    restored = mod.Snapshot.from_json(original.to_json())
    assert restored.symbols == original.symbols


def test_a_snapshot_without_deprecated_parameters_stays_strict():
    """A base snapshot from before FND-5542 has no field: excuse nothing."""
    raw = json.loads(snap(_STATE_STORE_BASE).to_json())
    for rec in raw["symbols"].values():
        del rec["deprecated_params"]
    base = mod.Snapshot.from_json(json.dumps(raw))
    assert blocking(mod.compare(base, snap(_STATE_STORE_HEAD))) == {
        "application_sdk.thing:create_app_handler_service"
    }


# ---------------------------------------------------------------------------
# Inherited methods (FND-4917)
# ---------------------------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> mod.Snapshot:
    """Build a snapshot from ``{relative path under application_sdk/: source}``."""
    root = tmp_path / "application_sdk"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent != root and not (path.parent / "__init__.py").exists():
            (path.parent / "__init__.py").write_text("")
        path.write_text(text)
    return mod.build_snapshot(tmp_path)


_BASE_ERRORS = """
class Base:
    pass

class StoreError(Base):
    @property
    def error_code(self): ...
    def describe(self, verbose=False): ...

class NotFound(StoreError):
    @property
    def error_code(self): ...
"""


def test_method_moved_to_a_shared_base_in_another_module_is_not_a_removal(
    tmp_path: Path,
):
    """The FND-4917 shape: copies replaced by one mixin the classes inherit."""
    base = _tree(tmp_path / "base", {"errors.py": _BASE_ERRORS})
    head = _tree(
        tmp_path / "head",
        {
            "mixins.py": "class _Mixin:\n    @property\n    def error_code(self): ...\n",
            "errors.py": (
                "from application_sdk.mixins import _Mixin\n\n"
                "class Base:\n    pass\n\n"
                "class StoreError(_Mixin, Base):\n"
                "    def describe(self, verbose=False): ...\n\n"
                "class NotFound(StoreError):\n    pass\n"
            ),
        },
    )
    assert mod.compare(base, head) == []
    assert "application_sdk.errors:NotFound.describe" in head.symbols


def test_inherited_method_carries_the_definers_signature(tmp_path: Path):
    """Narrowing the base's method still narrows every subclass that inherits it."""
    base = _tree(tmp_path / "base", {"errors.py": _BASE_ERRORS})
    head = _tree(
        tmp_path / "head",
        {
            "errors.py": (
                "class Base:\n    pass\n\n"
                "class StoreError(Base):\n"
                "    @property\n    def error_code(self): ...\n"
                "    def describe(self): ...\n\n"
                "class NotFound(StoreError):\n    pass\n"
            )
        },
    )
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.errors:StoreError.describe",
        "application_sdk.errors:NotFound.describe",
    }


def test_method_gone_from_class_and_bases_is_still_a_removal(tmp_path: Path):
    base = _tree(tmp_path / "base", {"errors.py": _BASE_ERRORS})
    head = _tree(
        tmp_path / "head",
        {
            "errors.py": (
                "class Base:\n    pass\n\n"
                "class StoreError(Base):\n"
                "    def describe(self, verbose=False): ...\n\n"
                "class NotFound(StoreError):\n    pass\n"
            )
        },
    )
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.errors:StoreError.error_code",
        "application_sdk.errors:NotFound.error_code",
    }


def test_an_init_dropped_in_favour_of_the_bases_is_still_a_finding(tmp_path: Path):
    """The AST cannot see a generated ``__init__`` earlier in the MRO, so it never assumes one is inherited."""
    base = _tree(
        tmp_path / "base",
        {
            "errors.py": (
                "class StoreError:\n    def __init__(self, message, *, key=None): ...\n\n"
                "class NotFound(StoreError):\n"
                "    def __init__(self, message, *, key=None): ...\n"
            )
        },
    )
    head = _tree(
        tmp_path / "head",
        {
            "errors.py": (
                "class StoreError:\n    def __init__(self, message, *, key=None): ...\n\n"
                "class NotFound(StoreError):\n    pass\n"
            )
        },
    )
    assert blocking(mod.compare(base, head)) == {
        "application_sdk.errors:NotFound.__init__"
    }


def test_inherited_methods_follow_a_reexported_class(tmp_path: Path):
    """``from application_sdk.pkg import NotFound`` keeps the inherited members too."""
    files = {
        "pkg/errors.py": (
            "class StoreError:\n    def describe(self): ...\n\n"
            "class NotFound(StoreError):\n    pass\n"
        ),
        "pkg/__init__.py": "from application_sdk.pkg.errors import NotFound\n",
    }
    head = _tree(tmp_path, files)
    assert "application_sdk.pkg:NotFound.describe" in head.symbols


def test_linearize_follows_c3_for_a_diamond():
    parents = {"D": ["B", "C"], "B": ["A"], "C": ["A"], "A": []}
    assert mod._linearize("D", parents, {}) == ["D", "B", "C", "A"]


def test_linearize_survives_a_cycle():
    parents = {"A": ["B"], "B": ["A"]}
    assert mod._linearize("A", parents, {})[0] == "A"


# ---------------------------------------------------------------------------
# Declaring ``__all__`` on a leaf module (FND-4259)
# ---------------------------------------------------------------------------

_LEAF_BASE = (
    "from application_sdk.constants import DEPLOYMENT_NAME\n"
    "from decimal import Decimal\n"
    "def handler(): ...\n"
)


def test_a_new_all_that_leaves_out_a_bound_import_is_advisory():
    """Without ``__all__`` every intra-package import was surface. Declaring one
    that leaves an import out does not unbind it, so the name still imports:
    report it, never block on it."""
    base = snap(_LEAF_BASE)
    head = snap(_LEAF_BASE + "__all__ = ['handler']\n")
    findings = mod.compare(base, head)
    assert blocking(findings) == set()
    assert advisory(findings) == {"application_sdk.thing:DEPLOYMENT_NAME"}
    assert {f.kind for f in findings} == {"undeclared"}


def test_a_new_all_that_also_drops_the_import_is_blocking():
    """Unbound means gone: the advisory pass covers a name that still imports."""
    base = snap(_LEAF_BASE)
    head = snap("def handler(): ...\n__all__ = ['handler']\n")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:DEPLOYMENT_NAME"}


def test_narrowing_an_existing_all_is_still_blocking():
    """A module that already declared ``__all__`` declared the name on purpose.
    Unlisting it, then deleting the import a release later, would remove it with
    no deprecation cycle; so unlisting is the removal."""
    base = snap(_LEAF_BASE + "__all__ = ['handler', 'DEPLOYMENT_NAME']\n")
    head = snap(_LEAF_BASE + "__all__ = ['handler']\n")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.thing:DEPLOYMENT_NAME"}


def test_an_unlisted_import_is_not_surface_once_all_is_declared():
    """After the release that declares ``__all__``, deleting the import is
    clean: the base no longer counts it."""
    base = snap(_LEAF_BASE + "__all__ = ['handler']\n")
    head = snap("def handler(): ...\n__all__ = ['handler']\n")
    assert mod.compare(base, head) == []


def _class_tree(root: Path, consumer: str) -> mod.Snapshot:
    pkg = root / "application_sdk"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "home.py").write_text("class Thing:\n    def go(self, x): ...\n")
    (pkg / "user.py").write_text(consumer)
    return mod.build_snapshot(root)


def test_an_undeclared_class_import_takes_its_members_with_it(tmp_path: Path):
    """A re-exported class carries its methods on the re-exporting module. When
    a new ``__all__`` leaves the class out, its members are advisory too."""
    base = _class_tree(tmp_path / "base", "from application_sdk.home import Thing\n")
    head = _class_tree(
        tmp_path / "head",
        "from application_sdk.home import Thing\n__all__: list[str] = []\n",
    )
    findings = mod.compare(base, head)
    assert blocking(findings) == set()
    assert advisory(findings) == {
        "application_sdk.user:Thing",
        "application_sdk.user:Thing.go",
    }


def test_a_deprecated_alias_of_a_class_covers_its_members(tmp_path: Path):
    """Replacing a re-exported class with a ``_DEPRECATED_CONSTANTS`` alias
    serves the class object itself, methods included."""
    base = _class_tree(tmp_path / "base", "from application_sdk.home import Thing\n")
    head = _class_tree(
        tmp_path / "head",
        "from application_sdk import _deprecation\n"
        "__all__: list[str] = []\n"
        "_DEPRECATED_CONSTANTS = {'Thing': ('application_sdk.home.Thing', 'moved')}\n"
        "__getattr__ = _deprecation.deprecated_module_getattr(\n"
        "    __name__, _DEPRECATED_CONSTANTS, remove_in='4.0.0'\n)\n",
    )
    assert mod.compare(base, head) == []


def test_an_alias_of_a_different_class_does_not_cover_the_old_members(
    tmp_path: Path,
):
    """The alias keeps only what its target has. Pointing ``Thing`` at a class
    with no ``go`` removes ``Thing.go``, however deprecated the alias is."""
    base = _class_tree(tmp_path / "base", "from application_sdk.home import Thing\n")
    head = _class_tree(
        tmp_path / "head",
        "from application_sdk import _deprecation\n"
        "__all__: list[str] = []\n"
        "_DEPRECATED_CONSTANTS = {'Thing': ('application_sdk.other.Other', 'moved')}\n"
        "__getattr__ = _deprecation.deprecated_module_getattr(\n"
        "    __name__, _DEPRECATED_CONSTANTS, remove_in='4.0.0'\n)\n",
    )
    (tmp_path / "head" / "application_sdk" / "other.py").write_text(
        "class Other:\n    def stop(self): ...\n"
    )
    head = mod.build_snapshot(tmp_path / "head")
    findings = mod.compare(base, head)
    assert blocking(findings) == {"application_sdk.user:Thing.go"}


def test_the_report_lists_undeclared_names_in_their_own_section():
    base = snap(_LEAF_BASE)
    head = snap(_LEAF_BASE + "__all__ = ['handler']\n")
    report = mod.render_report(
        mod.compare(base, head), base_label="v1.0.0", head_label="HEAD"
    )
    assert "left out of a new `__all__`" in report
    assert "private names" not in report
    assert "DEPLOYMENT_NAME" in report
