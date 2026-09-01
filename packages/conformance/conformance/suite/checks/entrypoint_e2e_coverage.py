"""T025 EntrypointWithoutE2ECoverage — a bundle entrypoint with no e2e suite.

Why this rule exists
--------------------
``T012 MissingE2ETestSuite`` asks only that ``tests/e2e/`` hold one collectable
test, on the agreed reasoning that e2e needs one *representative* run rather than
scenario-level coverage. On a bundle app that reads as "the crawler suite is
enough", and across the connector fleet it has meant exactly that: every
AE-driven full-DAG e2e exercises the metadata-extraction entrypoint, and the
second one — typically a query-history miner — is never run against a tenant by
anything.

Each bundle entrypoint is its own Automation Engine submit against its own DAG,
its own task queue, and its own served manifest. A green crawler leg is no
evidence at all about the miner's dispatch path, so "one representative run" has
to mean one per entrypoint here.

Scope: ``multi`` mode only
--------------------------
:func:`~conformance.suite.checks.entrypoint_alignment._contract_entrypoints.scan_contract`
distinguishes two shapes that both get called "multi-entrypoint", and only one is
uncovered:

* ``multi`` — ``app/generated/<ep>/manifest.json`` subdirs, one marketplace card
  each. Each entrypoint is submitted independently. **This rule's scope.**
* ``single`` + ``routes`` — one card, with secondary entrypoints invoked as DAG
  nodes via ``workflow_type: "<app>:<wire>"`` (the BLDX-1342 route/card split).
  The parent's own full-DAG run executes them, so they are covered transitively.
  Flagging them would be a false positive against a deliberate design — see
  ``atlan-metabase-app``, whose ``extract-lineage`` runs as a DAG node.

So a routed entrypoint is never flagged, and a single-entrypoint app never sees
this rule at all. A bundle with only one entrypoint is also skipped: T012 already
reports its missing tier, and this rule is about the asymmetry *between*
entrypoints, which needs at least two to exist.

What counts as covering an entrypoint
-------------------------------------
Any collectable test class under ``tests/e2e/`` that resolves to the entrypoint
by one of the three ways the harness itself accepts:

1. inheriting the generated base for it (``<Ep>GeneratedE2EBase``),
2. a class-level ``entrypoint = "<ep>"``,
3. a class-level ``manifest_path`` containing ``/generated/<ep>/``.

Resolution is syntactic and deliberately generous: a suite that sets the value
dynamically is not flagged only if one of the three forms is literally present,
and the miss direction is a false *negative*. A rule that nagged a repo which had
in fact wired its miner up would get suppressed wholesale, which costs more than
the occasional missed gap.

What does *not* count: a prerequisite DAG run
---------------------------------------------
Since FND-1157 one e2e suite can run several entrypoint DAGs against one
connection — ``BaseE2ETest.dag_runs``, a tuple of ``DAGSpec`` — so a miner suite
whose lineage resolution needs a crawler-written entity cache runs the crawler
DAG first. **That run is not coverage for the crawler**, and the decision is
deliberate rather than an oversight: a prerequisite exists to produce state, it
is graded against the *consuming* suite's intent, and the whole point of this
rule is that one entrypoint's green run is no evidence about another's. Counting
it would be precisely the false negative the rule prevents.

So the guidance stands as **one collectable class per entrypoint, which may run
prerequisite DAGs for others** — and holding that line needs no code here: a
``DAGSpec(manifest_path=...)`` is a keyword argument inside a call, not one of
the three class-body forms above, so ``_class_string_attrs`` never sees it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    is_collectable_test_file,
    is_test_class,
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T025 = "T025"

# Reuses T012's tier exemption: a repo that has declared it has no e2e tier to
# speak of should not then be asked for one suite per entrypoint.
_E2E_TIER = "e2e"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Return Python source files under ``tests/e2e/``.

    Recursive: a suite in a subdirectory still counts as coverage here even
    though the CI matrix only fans out over the flat layout. Under-reporting a
    covered entrypoint would make this rule wrong; the flat-layout requirement is
    a separate concern with its own signal.
    """
    base = root / "tests" / "e2e"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------------------
# Entrypoint resolution from a test class
# ---------------------------------------------------------------------------


def _string_value(node: ast.expr) -> str | None:
    """The literal str a node evaluates to, or None if it is not a plain literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _class_string_attrs(cls: ast.ClassDef, wanted: str) -> list[str]:
    """Literal string values assigned to ``wanted`` in *cls*'s own body."""
    found: list[str] = []
    for stmt in cls.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == wanted:
                value = _string_value(stmt.value) if stmt.value is not None else None
                if value is not None:
                    found.append(value)
    return found


def _base_names(cls: ast.ClassDef) -> set[str]:
    """Bare names of *cls*'s bases, including the tail of dotted ones."""
    names: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _generated_base_entrypoints(
    bases: set[str], candidates: frozenset[str]
) -> set[str]:
    """Entrypoints implied by inheriting ``<Ep>GeneratedE2EBase``.

    The generated class is named from the entrypoint in PascalCase, so
    ``miner`` -> ``MinerGeneratedE2EBase`` and ``extract-metadata`` ->
    ``ExtractMetadataGeneratedE2EBase``. Matching is case-insensitive on the
    de-punctuated name so a hyphenated or underscored entrypoint still resolves.
    """
    covered: set[str] = set()
    for ep in candidates:
        squashed = "".join(ch for ch in ep if ch.isalnum()).lower()
        if not squashed:
            continue
        for base in bases:
            if not base.endswith("GeneratedE2EBase"):
                continue
            prefix = base[: -len("GeneratedE2EBase")]
            if prefix.lower() == squashed:
                covered.add(ep)
    return covered


def _entrypoints_covered_by_class(
    cls: ast.ClassDef, candidates: frozenset[str]
) -> set[str]:
    """Which of *candidates* this test class declares it exercises."""
    covered: set[str] = set()

    for declared in _class_string_attrs(cls, "entrypoint"):
        if declared in candidates:
            covered.add(declared)

    for manifest_path in _class_string_attrs(cls, "manifest_path"):
        for ep in candidates:
            if f"/generated/{ep}/" in manifest_path:
                covered.add(ep)

    covered |= _generated_base_entrypoints(_base_names(cls), candidates)
    return covered


def _covered_entrypoints(paths: list[Path], candidates: frozenset[str]) -> set[str]:
    """Union of the entrypoints every collectable e2e test class resolves to."""
    covered: set[str] = set()
    for path in paths:
        if not is_collectable_test_file(path.name):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            # Unreadable or unparseable: contributes no coverage. A file the
            # collector cannot read is not evidence of a wired-up entrypoint.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and is_test_class(node):
                covered |= _entrypoints_covered_by_class(node, candidates)
    return covered


# ---------------------------------------------------------------------------
# Exemption
# ---------------------------------------------------------------------------


def _e2e_tier_exempt(root: Path) -> bool:
    """True when ``[tool.conformance].exempt_test_tiers`` lists the e2e tier.

    Mirrors ``test_structure._exempt_test_tiers`` rather than importing it: this
    check needs only the one tier, and a repo that opted out of having an e2e
    tier at all must not then be asked for one suite per entrypoint.
    """
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    tiers = data.get("tool", {}).get("conformance", {}).get("exempt_test_tiers")
    if not isinstance(tiers, list):
        return False
    return any(str(t).strip().lower() == _E2E_TIER for t in tiers)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _message(entrypoint: str, all_names: frozenset[str], covered: set[str]) -> str:
    covered_str = ", ".join(sorted(covered)) if covered else "none"
    return (
        f"Bundle entrypoint '{entrypoint}' has no e2e suite. This app declares "
        f"{len(all_names)} entrypoints ({', '.join(sorted(all_names))}) as "
        f"app/generated/<name>/ contract dirs, and tests/e2e/ covers: "
        f"{covered_str}. Each entrypoint is its own Automation Engine submit "
        f"against its own DAG and task queue, so a green run of another one is no "
        f"evidence about this one. Add tests/e2e/test_<app>_{entrypoint}_e2e.py "
        f"with a class inheriting the generated base for this entrypoint (the CI "
        f"matrix fans out one leg per file, so a second file is a second leg). If "
        f"'{entrypoint}' genuinely cannot be exercised against a tenant, suppress "
        f"it alone with '# conformance: ignore[{RULE_T025}:{entrypoint}] <reason>' "
        f"on the first line of pyproject.toml (a bare 'ignore[{RULE_T025}]' "
        f"suppresses every entrypoint's finding)."
    )


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T025 compares the contract against the whole tier; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Report each ``multi``-mode contract entrypoint with no e2e suite.

    Parameters
    ----------
    paths:
        Python source files under ``tests/e2e/`` (as returned by :func:`discover`).
    root:
        Repo root — used for the contract scan, the tier exemption, and the
        suppression anchor.

    Findings are anchored to ``pyproject.toml`` rather than to a test file,
    because the thing that is missing is a file: there is no line to point at.
    That also makes the suppression anchor the same one T011/T012 use. Each
    finding carries its entrypoint name as the discriminator, so the shared
    anchor does not collapse their identities: fingerprints stay distinct, and
    ``# conformance: ignore[T025:<entrypoint>]`` suppresses one entrypoint
    without suppressing the others.
    """
    scan = scan_contract(root)
    if scan.mode != "multi":
        # Single-entrypoint apps have an unambiguous target, and a route/card-split
        # app's secondary entrypoints run inside the parent's DAG. Neither has a
        # gap for this rule to report.
        return []

    if len(scan.names) < 2:
        # A bundle with one entrypoint has nothing this rule can say that T012
        # does not already say better: if it has no e2e suite, T012 reports the
        # missing tier, and two findings for one missing file is noise. This rule
        # is about the asymmetry between entrypoints, which needs at least two.
        return []

    if _e2e_tier_exempt(root):
        return []

    covered = _covered_entrypoints(paths, scan.names)
    missing = sorted(scan.names - covered)
    if not missing:
        return []

    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    suppressions = parse_toml_suppressions(text) if text else {}

    return [
        make_toml_finding(
            rule_id=RULE_T025,
            file="pyproject.toml",
            line=1,
            column=1,
            message=_message(entrypoint, scan.names, covered),
            suppressions=suppressions,
            # One finding per missing entrypoint, all anchored to the same
            # pyproject.toml:1. The entrypoint name is the finding's identity:
            # it keys the SARIF fingerprint (so per-entrypoint dedup and
            # oscillation tracking can tell "crawler missing" from "miner
            # missing") and the `# conformance: ignore[T025:<entrypoint>]`
            # suppression form (so one entrypoint can be exempted while the
            # others stay reported).
            discriminator=entrypoint,
        )
        for entrypoint in missing
    ]


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T025: report bundle (multi-entrypoint) contract entrypoints that no "
        "tests/e2e/ suite exercises."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
