"""Every self-reference to this repo's actions and reusable workflows is @main.

Policy and rationale: "Self-references are `@main`" in docs/standards/ci.md
(FND-4249). Before it, most self-references said @main while sdr-e2e called
setup-deps at a release tag that Renovate bumped each release, so one job ran
two revisions of this repo. Renovate never bumps a branch ref, so once
everything says @main this test is what keeps a tag or SHA from creeping back.

It also fails on a self-reference to an action or workflow that does not exist
in this tree, so deleting a reusable without updating its callers and usage
examples is red here rather than on a consumer's next run. It only sees
callers in this tree: a reusable called by a retired bootstrap shim that
lingers in consumer repos has no caller here, and
test_reusable_retirement.py guards that case (FND-4654).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Where self-references live: workflows, composite actions, workflow
# templates, scripts that render workflow text, the whole conformance package
# (bootstrap templates it scaffolds into connector repos, rule help text,
# README), and the docs that show consumers how to call us.
SCAN_ROOTS = (
    ".github",
    "packages/conformance",
    "docs",
)
SCAN_SUFFIXES = {".yaml", ".yml", ".md", ".py", ".sh", ".pkl", ".json"}
# Test fixtures deliberately contain off-policy refs (e.g. C001's own cases,
# which check that a tag-pinned atlanhq/ ref is exempt); they are inputs, not
# references.
EXCLUDED_DIRS = (".github/scripts/tests", "packages/conformance/tests")

SELF_REF = re.compile(
    r"atlanhq/application-sdk/\.github/(?P<kind>actions|workflows)/"
    # The ref stops at anything that can follow it in YAML, Markdown or a
    # Python string literal: whitespace, quotes, backticks, comments,
    # brackets, commas and the backslash of an escaped "\\n".
    r"(?P<name>[A-Za-z0-9_.\-/]+?)@(?P<ref>[^\s\"'`#)\],\\]+)"
)

ALLOWED_REF = "main"

# (file, target) pairs that name a missing target on purpose. Each is history,
# not a call: keep the reason beside the entry.
MISSING_TARGET_EXEMPT = {
    # Explains why the retired docstring-coverage shim always failed: the
    # reusable it called never existed.
    ("packages/conformance/conformance/bootstrap/render.py", "docstring-coverage.yaml"),
}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if any(rel.startswith(d + "/") for d in EXCLUDED_DIRS):
                continue
            files.append(path)
    return files


def _self_refs() -> list[tuple[str, int, re.Match[str]]]:
    refs: list[tuple[str, int, re.Match[str]]] = []
    for path in _scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            refs.extend((rel, lineno, m) for m in SELF_REF.finditer(line))
    return refs


def _target_exists(kind: str, name: str) -> bool:
    if kind == "workflows":
        return (REPO_ROOT / ".github/workflows" / name).is_file()
    action_dir = REPO_ROOT / ".github/actions" / name
    return any((action_dir / f).is_file() for f in ("action.yaml", "action.yml"))


def test_scan_finds_self_references() -> None:
    # Guards the guard: a regex or path change that matched nothing would
    # make both checks below pass vacuously.
    refs = _self_refs()
    assert len(refs) > 20, f"expected many self-references, found {len(refs)}"
    assert any(rel.startswith(".github/workflows/") for rel, _, _ in refs)
    assert any(rel.startswith(".github/actions/") for rel, _, _ in refs)


def test_every_self_reference_is_main() -> None:
    offenders = [
        f"{rel}:{lineno}: {m.group(0)}"
        for rel, lineno, m in _self_refs()
        if m.group("ref") != ALLOWED_REF
    ]
    assert not offenders, (
        "application-sdk self-references must use @main "
        "(docs/standards/ci.md, 'Self-references are `@main`'):\n  "
        + "\n  ".join(offenders)
    )


def test_every_self_reference_targets_an_existing_file() -> None:
    missing = [
        f"{rel}:{lineno}: {m.group(0)}"
        for rel, lineno, m in _self_refs()
        if not _target_exists(m.group("kind"), m.group("name"))
        and (rel, m.group("name")) not in MISSING_TARGET_EXEMPT
    ]
    assert not missing, (
        "self-references to actions/workflows that do not exist in this repo "
        "(deleted or renamed? update the caller or example):\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize(
    ("line", "ref"),
    [
        ("uses: atlanhq/application-sdk/.github/actions/setup-deps@main", "main"),
        ("uses: atlanhq/application-sdk/.github/actions/setup-deps@v3.53.2", "v3.53.2"),
        (
            '        uses: "atlanhq/application-sdk/.github/actions/trivy-container@main"',
            "main",
        ),
        (
            "uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@abc123 # v1",
            "abc123",
        ),
        ("`atlanhq/application-sdk/.github/actions/sdr-e2e@main`)", "main"),
        (
            '"  uses: atlanhq/application-sdk/.github/actions/setup-deps@main\\n"',
            "main",
        ),
        ("[atlanhq/application-sdk/.github/actions/setup-deps@main, other]", "main"),
    ],
)
def test_pattern_extracts_ref(line: str, ref: str) -> None:
    match = SELF_REF.search(line)
    assert match is not None
    assert match.group("ref") == ref
