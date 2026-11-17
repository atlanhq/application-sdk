"""The shared per-file ``scan_path`` factory and the helpers folded into ``_ast_common``.

``scan_path`` was the same read-guard-plus-relative-URI body in two dozen check
modules, and SDR-app detection, SDK ``App``-alias collection and the JSON string
walk were each copied two or three times.  These tests pin the shared helpers'
behaviour and keep the copies from coming back.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

import pytest

from conformance.suite.checks._ast_common import (
    is_sdr_app,
    iter_strings,
    make_scan_path,
    read_source,
    relative_uri,
    sdk_app_import_aliases,
)
from conformance.suite.schema.findings import Finding

_CHECKS_ROOT = Path(__file__).resolve().parents[1] / "conformance" / "suite" / "checks"


def _echo(text: str, uri: str, **context: object) -> list[Finding]:
    """A ``scan_text`` stand-in that reports what it was called with."""
    message = f"{text}|{sorted(context.items())}"
    return [Finding(rule_id="X001", file=uri, line=1, column=1, message=message)]


# ── make_scan_path ───────────────────────────────────────────────────────────


def test_scan_path_reports_the_root_relative_uri(tmp_path: Path) -> None:
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("body", encoding="utf-8")

    [finding] = make_scan_path(_echo)(target, tmp_path)

    assert finding.file == str(Path("pkg") / "mod.py")
    assert finding.message == "body|[]"


def test_scan_path_keeps_a_path_outside_root(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("x", encoding="utf-8")

    [finding] = make_scan_path(_echo)(target, tmp_path / "elsewhere")

    assert finding.file == str(target)


@pytest.mark.parametrize("content", [b"\xff\xfe broken", None])
def test_scan_path_skips_an_unreadable_file(
    tmp_path: Path, content: bytes | None
) -> None:
    target = tmp_path / "mod.py"
    if content is not None:
        target.write_bytes(content)

    assert make_scan_path(_echo)(target, tmp_path) == []


def test_scan_path_passes_context_only_after_a_successful_read(tmp_path: Path) -> None:
    calls: list[tuple[Path, Path]] = []

    def context(path: Path, root: Path) -> Mapping[str, object]:
        calls.append((path, root))
        return {"flag": 7}

    scan_path = make_scan_path(_echo, context=context)
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe")
    good = tmp_path / "good.py"
    good.write_text("ok", encoding="utf-8")

    assert scan_path(tmp_path / "bad.py", tmp_path) == []
    assert calls == []
    [finding] = scan_path(good, tmp_path)
    assert finding.message == "ok|[('flag', 7)]"
    assert calls == [(good, tmp_path)]


def test_scan_path_is_named_as_the_module_attribute() -> None:
    assert make_scan_path(_echo).__qualname__ == "scan_path"


def test_read_source_and_relative_uri(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("t", encoding="utf-8")

    assert read_source(target, tmp_path) == ("t", "a.py")
    assert read_source(tmp_path / "missing.py", tmp_path) is None
    assert relative_uri(target, tmp_path / "other") == str(target)


# ── is_sdr_app ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("atlan_yaml", "expected"),
    [
        ("self_deployed_runtime: true\n", True),
        ("name: x\nSelf_Deployed_Runtime: TRUE\n", True),
        ("self_deployed_runtime: false\n", False),
        ("  self_deployed_runtime: true\n", False),
        ("name: x\n", False),
    ],
)
def test_is_sdr_app_reads_the_flag(
    tmp_path: Path, atlan_yaml: str, expected: bool
) -> None:
    (tmp_path / "atlan.yaml").write_text(atlan_yaml, encoding="utf-8")
    assert is_sdr_app(tmp_path) is expected


def test_is_sdr_app_is_false_without_a_readable_atlan_yaml(tmp_path: Path) -> None:
    assert is_sdr_app(tmp_path) is False
    (tmp_path / "atlan.yaml").write_bytes(b"\xff\xfeself_deployed_runtime: true\n")
    assert is_sdr_app(tmp_path) is False


# ── sdk_app_import_aliases ───────────────────────────────────────────────────


def test_sdk_app_import_aliases_collects_sdk_imports_only() -> None:
    tree = ast.parse(
        "from application_sdk.app import App as Base\n"
        "from application_sdk.templates import SqlApp\n"
        "from application_sdk import Other\n"
        "from vendor import App\n"
        "from .local import SqlMetadataExtractor\n"
        "def f():\n"
        "    from application_sdk.templates import SqlQueryExtractor\n"
    )
    assert sdk_app_import_aliases(tree) == frozenset(
        {"Base", "SqlApp", "SqlQueryExtractor"}
    )


# ── iter_strings ─────────────────────────────────────────────────────────────


def test_iter_strings_walks_every_string_leaf() -> None:
    value = {"a": "x", "b": [1, "y", {"c": "z", "d": None}], "e": 2.0}
    assert list(iter_strings(value)) == ["x", "y", "z"]


# ── the copies stay folded ───────────────────────────────────────────────────

#: Helpers that now live only in ``_ast_common``.
_FOLDED = frozenset({"_is_sdr_app", "_sdk_app_aliases", "_iter_strings"})

#: A ``scan_path`` body that still reads and relativises by hand, for a reason
#: the factory does not cover: each reports a POSIX URI on every platform, and
#: the first two scan a file that may not exist.
_HAND_WRITTEN_SCAN_PATH = frozenset(
    {"bootstrap_drift.py", "gitignore_entries.py", "retry_ladder.py"}
)


def _check_modules() -> list[Path]:
    return sorted(
        p
        for p in _CHECKS_ROOT.rglob("*.py")
        if "_ast_common" not in p.relative_to(_CHECKS_ROOT).parts
    )


def test_folded_helpers_are_not_redefined() -> None:
    offenders = [
        f"{path.relative_to(_CHECKS_ROOT)}:{node.lineno} {node.name}"
        for path in _check_modules()
        for node in ast.parse(path.read_bytes()).body
        if isinstance(node, ast.FunctionDef) and node.name in _FOLDED
    ]
    assert offenders == [], "use the _ast_common helper instead: " + ", ".join(
        offenders
    )


def test_scan_path_is_not_hand_rolled() -> None:
    """A module-level ``scan_path`` that relativises a path itself duplicates the factory."""
    offenders: list[str] = []
    for path in _check_modules():
        rel = path.relative_to(_CHECKS_ROOT).as_posix()
        if rel in _HAND_WRITTEN_SCAN_PATH:
            continue
        for node in ast.parse(path.read_bytes()).body:
            if not (isinstance(node, ast.FunctionDef) and node.name == "scan_path"):
                continue
            if any(
                isinstance(sub, ast.Attribute) and sub.attr == "relative_to"
                for sub in ast.walk(node)
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], "use make_scan_path/read_source: " + ", ".join(offenders)
