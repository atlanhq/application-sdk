"""Every repo path a test reads from another area runs that test (FND-4907).

``sdk-gate.yaml`` decides which test jobs run from ``dorny/paths-filter``
globs. A drift guard that reads a file from a *different* area — a toolkit
example, a canonical ``.github/`` script the conformance package vendors — is
only as good as the filter that gates its job: if the filter lists where the
test lives but not what it reads, a PR that edits only the input merges with
the guard never run, and the drift surfaces on some later, unrelated PR.

This module checks the three such reads the audit found, each derived from
the source rather than restated, so a new read is covered or fails here:

* the canonical sources of ``MANAGED_ACTION_FILES`` (parsed from
  ``render.py``) must run ``conformance-suite-tests``;
* every ``contract-toolkit/examples/...`` path an SDK or conformance-package
  test builds must run that test's job;
* every ``application_sdk`` module a generated toolkit example imports must
  run the toolkit suite, whose ``test-sdk-import.py`` imports those examples.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / ".github" / "workflows" / "sdk-gate.yaml"
RENDER = (
    REPO_ROOT / "packages" / "conformance" / "conformance" / "bootstrap" / "render.py"
)
EXAMPLES = REPO_ROOT / "contract-toolkit" / "examples"

# The path filters whose match runs each test job (the job `if:`s in
# sdk-gate.yaml). conformance-suite-tests also runs on `sdk`.
SDK_TESTS = ("sdk",)
CONFORMANCE_SUITE_TESTS = ("conformance_pkg", "sdk")
TOOLKIT_TESTS = ("toolkit",)

# Test trees and the job that runs them.
TEST_TREES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tests", SDK_TESTS),
    ("packages/conformance/tests", CONFORMANCE_SUITE_TESTS),
)

# Reads whose path ends in a non-literal (an imported constant), so discovery
# below sees only a prefix. Keyed by test file; each value must extend the
# discovered prefix, so a moved read cannot keep a stale entry here.
RESOLVED_READS: dict[str, tuple[str, ...]] = {
    "tests/unit/validation/test_artifact_sources.py": (
        "contract-toolkit/examples/artifact-schemas/app/generated/artifact_schemas.json",
    ),
}


@cache
def _filters() -> dict[str, list[str]]:
    gate: dict[str, Any] = yaml.safe_load(GATE.read_text(encoding="utf-8"))
    jobs: dict[str, dict[str, Any]] = gate["jobs"]
    for job in jobs.values():
        steps: list[dict[str, Any]] = job.get("steps") or []
        for step in steps:
            if "paths-filter" in str(step.get("uses", "")):
                filters: dict[str, list[str]] = yaml.safe_load(step["with"]["filters"])
                return filters
    raise AssertionError("no dorny/paths-filter step in sdk-gate.yaml")


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate the glob subset sdk-gate.yaml uses (picomatch, dot: true).

    ``**`` spans path segments, ``*`` and ``?`` stay within one. Negations are
    not supported, and sdk-gate.yaml must not use them (see its `sdk` note).
    """
    assert not pattern.startswith("!"), f"negated filter pattern: {pattern}"
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out += "(?:/.*)?"
            i += 3
        elif pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _covering(path: str, areas: tuple[str, ...]) -> list[str]:
    filters = _filters()
    return [
        f"{area}: {pattern}"
        for area in areas
        for pattern in filters[area]
        if _glob_regex(pattern).match(path)
    ]


def _files_under(rel: str) -> list[str]:
    path = REPO_ROOT / rel
    if path.is_file():
        return [rel]
    return sorted(
        p.relative_to(REPO_ROOT).as_posix() for p in path.rglob("*") if p.is_file()
    )


def _uncovered(rel: str, areas: tuple[str, ...]) -> list[str]:
    assert (REPO_ROOT / rel).exists(), f"{rel} does not exist"
    return [f for f in _files_under(rel) if not _covering(f, areas)]


# ---------------------------------------------------------------------------
# The matcher itself: a matcher that matched everything would pass every test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("application_sdk/**", "application_sdk/a/b.py", True),
        ("application_sdk/**", "application_sdk_x/b.py", False),
        ("application_sdk/testing/e2e/**", "application_sdk/testing/e2e.py", False),
        ("**/*.py", "a.py", True),
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "a/b/c.pyi", False),
        (
            ".github/workflows/contract-toolkit-*.yml",
            ".github/workflows/contract-toolkit-x.yml",
            True,
        ),
        (
            ".github/workflows/contract-toolkit-*.yml",
            ".github/workflows/contract-toolkit-x/y.yml",
            False,
        ),
        ("pyproject.toml", "packages/api/pyproject.toml", False),
        (
            ".github/scripts/probe_code_scanning.py",
            ".github/scripts/probe_code_scanning.py",
            True,
        ),
    ],
)
def test_glob_matcher(pattern: str, path: str, expected: bool) -> None:
    assert bool(_glob_regex(pattern).match(path)) is expected


# ---------------------------------------------------------------------------
# Guard 1: the canonical sources of vendored bootstrap templates
# ---------------------------------------------------------------------------


def _managed_action_destinations() -> list[str]:
    tree = ast.parse(RENDER.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MANAGED_ACTION_FILES"
            and node.value is not None
        ):
            return [dest for dest, _ in ast.literal_eval(node.value)]
    raise AssertionError(f"MANAGED_ACTION_FILES not found in {RENDER}")


def test_vendored_template_sources_run_the_template_guard() -> None:
    """test_bootstrap.py byte-compares each template with its canonical file.

    Entries with no canonical file here are template-only; test_bootstrap.py
    names them and fails if one is missing unexpectedly, so skipping them here
    is not a gap.
    """
    canonical = [d for d in _managed_action_destinations() if (REPO_ROOT / d).is_file()]
    assert len(canonical) >= 3, f"expected the vendored pairs, found {canonical}"
    missing = [d for d in canonical if not _covering(d, CONFORMANCE_SUITE_TESTS)]
    assert not missing, (
        f"{missing} are vendored by bootstrap (MANAGED_ACTION_FILES) but match no "
        "`conformance_pkg` filter in sdk-gate.yaml, so a PR editing only the "
        "canonical file merges without the drift guard. Add each path there."
    )


# ---------------------------------------------------------------------------
# Guard 2: toolkit examples read by SDK and conformance-package tests
# ---------------------------------------------------------------------------

# `"contract-toolkit" / "examples" / "a" / "b" ...`, across line breaks; the
# chain stops at the first non-literal segment.
_EXAMPLE_READ = re.compile(
    r'"contract-toolkit"\s*/\s*"examples"((?:\s*/\s*"[^"]+")*)(\s*/\s*[A-Za-z_])?'
)
_SEGMENT = re.compile(r'"([^"]+)"')


def _discovered_example_reads() -> list[tuple[str, str, bool, tuple[str, ...]]]:
    """(test file, read path, ends in a non-literal, areas) per read."""
    reads: list[tuple[str, str, bool, tuple[str, ...]]] = []
    for tree, areas in TEST_TREES:
        for test in sorted((REPO_ROOT / tree).rglob("*.py")):
            text = test.read_text(encoding="utf-8")
            for m in _EXAMPLE_READ.finditer(text):
                segments = _SEGMENT.findall(m.group(1))
                path = "/".join(["contract-toolkit", "examples", *segments])
                reads.append(
                    (
                        test.relative_to(REPO_ROOT).as_posix(),
                        path,
                        m.group(2) is not None,
                        areas,
                    )
                )
    return reads


def test_discovery_finds_the_known_reads() -> None:
    """Non-vacuous: the reads the audit found are all seen by discovery."""
    seen = {(test, path) for test, path, _, _ in _discovered_example_reads()}
    assert {
        (
            "tests/unit/validation/test_artifact_sources.py",
            "contract-toolkit/examples/artifact-schemas/app/generated",
        ),
        (
            "packages/conformance/tests/test_generated_freshness.py",
            "contract-toolkit/examples/bundle/app.pkl",
        ),
        (
            "packages/conformance/tests/test_generated_freshness.py",
            "contract-toolkit/examples/system-app",
        ),
    } <= seen, sorted(seen)


def test_toolkit_examples_read_by_tests_run_those_tests() -> None:
    problems: list[str] = []
    for test, path, partial, areas in _discovered_example_reads():
        if partial:
            resolved = RESOLVED_READS.get(test, ())
            if not resolved or not all(r.startswith(path + "/") for r in resolved):
                problems.append(
                    f"{test} reads under {path}/ through a non-literal; list the "
                    "concrete path(s) in RESOLVED_READS"
                )
                continue
            targets = list(resolved)
        else:
            targets = [path]
        for target in targets:
            if not (REPO_ROOT / target).exists():
                problems.append(f"{test} reads {target}, which does not exist")
                continue
            uncovered = _uncovered(target, areas)
            if uncovered:
                problems.append(
                    f"{test} reads {target}, but {uncovered[:3]} match none of "
                    f"the {list(areas)} filters in sdk-gate.yaml"
                )
    assert not problems, "\n".join(problems)


def test_resolved_reads_are_still_read() -> None:
    """A RESOLVED_READS entry whose test no longer reads there is stale."""
    partial = {
        test for test, _, is_partial, _ in _discovered_example_reads() if is_partial
    }
    assert set(RESOLVED_READS) <= partial, sorted(set(RESOLVED_READS) - partial)


# ---------------------------------------------------------------------------
# Guard 3: SDK modules imported by generated toolkit examples
# ---------------------------------------------------------------------------


def _example_sdk_modules() -> set[str]:
    modules: set[str] = set()
    for py in EXAMPLES.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            modules.update(
                n
                for n in names
                if n == "application_sdk" or n.startswith("application_sdk.")
            )
    return modules


def _module_file(module: str) -> str:
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    raise AssertionError(f"{module} imported by a toolkit example does not exist")


def test_sdk_modules_imported_by_toolkit_examples_run_the_toolkit_suite() -> None:
    modules = _example_sdk_modules()
    assert "application_sdk.testing.e2e" in modules, sorted(modules)
    missing = sorted(
        f"{m} ({_module_file(m)})"
        for m in modules
        if not _covering(_module_file(m), TOOLKIT_TESTS)
    )
    assert not missing, (
        f"generated toolkit examples import {missing}, but no `toolkit` filter in "
        "sdk-gate.yaml matches the module, so a change there skips "
        "contract-toolkit/scripts/test-sdk-import.py on the PR. Add the path."
    )
