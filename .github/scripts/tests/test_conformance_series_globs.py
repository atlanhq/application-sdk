"""Every conformance matrix leg must watch the trees its own checkers read.

The conformance suite is scheduled per series, and each leg is gated by a
``dorny/paths-filter`` glob (``matrix.paths``).  A leg whose glob does not cover
a tree its checkers discover from is **silently skipped** on exactly the PRs
that change that tree — the rules do not fire, nothing goes red, and the gap is
invisible in the run summary because the leg reports success.

That is not hypothetical.  The T-series glob was ``{tests/**,**/pyproject.toml}``
while four of its checkers discover from ``.github/``, three of them reading
nothing else.  A consumer PR that only edited ``.github/workflows/tests.yaml``
— dropping the reusable caller, flipping ``enable-e2e`` to false — matched no
filter path, so T020-T022 never ran on the one diff shape they exist to catch.

A comment cannot hold that invariant: the glob and the checkers live in
different files, in different languages, and drift independently.  This test
ties them together, derived from the checkers rather than from a hand-kept list,
so a NEW checker that reads an unwatched tree fails here instead of going quiet
in production.

Scope: the discovery *roots* a checker reads, not the exact file set — matching
picomatch's full semantics in Python would be its own source of drift.  The
property asserted is structural: for each root a series reads, the leg's glob
contains an alternative anchored at that root.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFORMANCE = REPO_ROOT / ".github" / "workflows" / "conformance-reusable.yaml"
CHECKS_DIR = REPO_ROOT / "packages" / "conformance" / "conformance" / "suite" / "checks"

#: Discovery roots a checker can read, and the path prefix a leg's glob must
#: anchor an alternative at to cover each one.  Keyed by source markers that
#: indicate the checker reads that root.
#:
#: Detection is deliberately **generous**: the markers match message strings and
#: docstrings as well as real filesystem reads.  The asymmetry is the point — a
#: false positive costs one extra alternative in a glob (the leg runs slightly
#: more often), while a false negative costs a silently skipped leg, which is the
#: bug this test exists to prevent.  Err toward demanding coverage.
#:
#: ``/ "generated"`` catches the loop form
#: (``for parent in ("app", "contract"): root / parent / "generated"``) that
#: e2e_generated_harness uses, which no literal path string would match.  It
#: demands both roots, mirroring the P-series glob.
_ROOT_MARKERS: dict[str, tuple[str, ...]] = {
    ".github": ('root / ".github"', '".github/', '".github"'),
    "app/generated": ('"app" / "generated"', '"app/generated', '/ "generated"'),
    "contract": ('root / "contract"', '"contract/', '/ "generated"'),
}


def _series_of(source: str) -> str | None:
    """The ``SERIES`` constant a check module declares, if any."""
    match = re.search(r'^SERIES\s*=\s*"([A-Z])"', source, re.MULTILINE)
    return match.group(1) if match else None


def _roots_read(source: str) -> set[str]:
    """Discovery roots *source* demonstrably reads."""
    return {
        root
        for root, markers in _ROOT_MARKERS.items()
        if any(marker in source for marker in markers)
    }


def _glob_alternatives(paths_glob: str) -> list[str]:
    """Split a picomatch glob into its top-level brace alternatives.

    ``"{tests/**,**/pyproject.toml,.github/**}"`` ->
    ``["tests/**", "**/pyproject.toml", ".github/**"]``.  A glob with no braces
    is a single alternative.  Nesting is not expanded — none of the legs use it,
    and a nested brace would still surface here as a literal that fails to match
    the expected prefix rather than passing silently.
    """
    glob = paths_glob.strip()
    if glob.startswith("{") and glob.endswith("}"):
        glob = glob[1:-1]
    return [part.strip() for part in glob.split(",") if part.strip()]


def _covers_root(paths_glob: str, root: str) -> bool:
    """Whether *paths_glob* has an alternative anchored at *root*.

    ``**/...`` alternatives are deliberately NOT treated as covering a root:
    picomatch does not match a leading dot with a wildcard, so ``**/*.yaml``
    never matches ``.github/workflows/tests.yaml``.  That subtlety is precisely
    how the T-series gap survived review, so it is asserted rather than assumed.
    """
    return any(
        alternative == root or alternative.startswith(root + "/")
        for alternative in _glob_alternatives(paths_glob)
    )


@pytest.fixture(scope="module")
def legs() -> dict[str, str]:
    """``{series letter: paths glob}`` for every leg in the conformance matrix."""
    workflow = yaml.safe_load(CONFORMANCE.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["suite"]["strategy"]["matrix"]["include"]
    return {leg["series"]: leg["paths"] for leg in matrix if "paths" in leg}


@pytest.fixture(scope="module")
def roots_by_series() -> dict[str, set[str]]:
    """``{series letter: roots its checkers read}``, derived from check sources."""
    out: dict[str, set[str]] = {}
    for module in sorted(CHECKS_DIR.rglob("*.py")):
        if "__pycache__" in module.parts:
            continue
        source = module.read_text(encoding="utf-8")
        series = _series_of(source)
        if series is None:
            continue
        roots = _roots_read(source)
        if roots:
            out.setdefault(series, set()).update(roots)
    return out


def test_series_globs_cover_their_checkers(
    legs: dict[str, str], roots_by_series: dict[str, set[str]]
) -> None:
    """No leg may be gated on a glob blind to a tree its own checkers read."""
    assert roots_by_series, "no check modules found — CHECKS_DIR path is wrong"

    gaps: list[str] = []
    for series, roots in sorted(roots_by_series.items()):
        glob = legs.get(series)
        if glob is None:
            continue  # series with no filtered leg (runs unconditionally)
        for root in sorted(roots):
            if not _covers_root(glob, root):
                gaps.append(
                    f"{series}-series glob {glob!r} does not watch {root!r}, "
                    f"which its checkers discover from — PRs touching only that "
                    f"tree skip the leg silently"
                )
    assert not gaps, "\n".join(gaps)


def test_t_series_watches_dot_github(legs: dict[str, str]) -> None:
    """Pin the specific regression: T020-T022 read only ``.github/workflows/``.

    Kept as its own case so the failure names the rules at stake rather than
    only the generic invariant.
    """
    assert _covers_root(
        legs["T"], ".github"
    ), "T-series leg would skip a workflow-only PR — the diff shape T020-T022 grade"


def test_wildcard_prefix_does_not_count_as_covering_a_dot_root() -> None:
    """The helper must not accept ``**/…`` as covering ``.github``.

    picomatch does not match a leading dot with a wildcard, so treating
    ``**/*.yaml`` as coverage would make this whole test vacuous — it would have
    passed on the broken glob.
    """
    assert not _covers_root("{tests/**,**/pyproject.toml}", ".github")
    assert not _covers_root("**/*.yaml", ".github")
    assert _covers_root("{tests/**,.github/**}", ".github")
    assert _covers_root(".github/**", ".github")
