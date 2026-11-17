"""D011 — the app's declaration of the conformance suite itself."""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks._ast_common import (
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    CONFORMANCE_PACKAGE,
    RULE_D011,
    _is_floating_range,
    _is_self_check,
    _iter_dep_entries,
    _iter_dependency_group_entries,
    _make_finding,
    _normalise_name,
    _parse_requirement,
    _project_name,
)
from ._duckdb import (
    _lock_is_readable,
    _locked_package_version,
)


def _scan_conformance_dependency(
    text: str, rel_pyproject: str, root: Path
) -> list[Finding]:
    """D011: the app's declaration of the conformance suite itself.

    Repo-level, not per-file: this is a property of the root ``pyproject.toml``,
    so a monorepo must not collect one finding per sub-package.  Four branches,
    checked in order and reported at most once:

    1. **Undeclared** — the package appears in no dependency array, so
       ``uv run atlan-application-sdk-conformance`` (the form the remediation
       loop and the bootstrapped ``remediate`` skill use) fails to spawn.
    2. **Declared in** ``[project.dependencies]`` — a *placement* violation,
       not a spawn one: ``uv run`` does find the script, so no other branch
       fires, but a production sync installs ``[project.dependencies]`` and
       skips dependency groups, so the declaration ships a dev-only tool in
       the runtime image.  Reported even when a correct dev-group entry also
       exists, because the runtime line still has to go.
    3. **Pinned or uncapped** — declared, but with a specifier that cannot
       float (``==0.13.0``, ``~=0.17``) or one with no upper bound at all (a
       bare ``>=0.17.0``).  The D-series CI leg resolves the suite out of the
       app's ``uv.lock``, so a frozen specifier freezes the ruleset grading the
       repo, and an uncapped one admits an unreviewed major.  A floor is
       optional — ``<=1.0.0`` and ``>=0.17.0,<1.0.0`` both float.
    4. **Missing from the lock** — declared and floating, but absent from a
       readable ``uv.lock``.  CI syncs from the lock, so a pyproject-only edit
       leaves the console script unavailable in the very environment that needs
       it.  Skipped entirely when the lock is absent or unparseable, so a
       malformed lock never manufactures a finding.
    """
    # App-scoped: the SDK *publishes* this package, so it has no reason to
    # declare it as a consumer.
    if _is_self_check(_project_name(text)):
        return []

    suppressions = parse_toml_suppressions(text)
    conformance_norm = _normalise_name(CONFORMANCE_PACKAGE)
    canonical = f"{CONFORMANCE_PACKAGE}<=1.0.0"

    entries = [
        e
        for e in (*_iter_dep_entries(text), *_iter_dependency_group_entries(text))
        if e.name == conformance_norm
    ]

    # ── Branch 1: undeclared ────────────────────────────────────────────────
    if not entries:
        # Anchor at the [dependency-groups] header so the reviewer sees where
        # the declaration belongs; line 1 when there is no such table.
        anchor_line = 1
        for ln, line in enumerate(text.splitlines(), start=1):
            if line.strip() == "[dependency-groups]":
                anchor_line = ln
                break
        return [
            _make_finding(
                rule_id=RULE_D011,
                file=rel_pyproject,
                line=anchor_line,
                column=1,
                message=(
                    f"App pyproject.toml does not declare "
                    f"'{CONFORMANCE_PACKAGE}' in any dependency group. The "
                    f"remediation loop invokes the suite as 'uv run "
                    f"{CONFORMANCE_PACKAGE}', which fails to spawn when the "
                    f"package is undeclared, so findings can be reported by CI "
                    f"but never remediated in the repo. Add '{canonical}' to "
                    f"[dependency-groups].dev — not to [project.dependencies], "
                    f"which would ship it in the runtime image."
                ),
                suppressions=suppressions,
            )
        ]

    # ── Branch 2: declared in the runtime array ─────────────────────────────
    # A floating entry in [project.dependencies] satisfies every other branch
    # — the console script really does spawn — so placement has to be graded
    # on its own or the one array the package must never appear in is the one
    # array this rule never reports.
    runtime_entries = [e for e in entries if e.array_path == "project.dependencies"]
    if runtime_entries:
        entry = runtime_entries[0]
        return [
            _make_finding(
                rule_id=RULE_D011,
                file=rel_pyproject,
                line=entry.line,
                column=entry.column,
                message=(
                    f"'{CONFORMANCE_PACKAGE}' is declared in "
                    f"[project.dependencies]. That ships a dev-only tool in "
                    f"the runtime image: a production sync installs "
                    f"[project.dependencies] and skips dependency groups. "
                    f"Move it into [dependency-groups].dev as '{canonical}' "
                    f"(any dev dependency group or "
                    f"[project.optional-dependencies.*] array is accepted) "
                    f"and delete the [project.dependencies] entry — even if a "
                    f"correct dev-group entry already exists, the runtime one "
                    f"still has to go."
                ),
                suppressions=suppressions,
            )
        ]

    # ── Branch 3: declared, but the specifier cannot float ──────────────────
    for entry in entries:
        parsed = _parse_requirement(entry.raw)
        spec = parsed[1] if parsed else ""
        if not _is_floating_range(spec):
            shown = spec or "no version specifier"
            return [
                _make_finding(
                    rule_id=RULE_D011,
                    file=rel_pyproject,
                    line=entry.line,
                    column=entry.column,
                    message=(
                        f"'{CONFORMANCE_PACKAGE}' is declared in "
                        f"[{entry.array_path}] with a specifier that cannot "
                        f"float ({shown}). The D-series CI leg resolves the "
                        f"suite from this repo's uv.lock, so an exact pin or a "
                        f"'~=' compatible-release pin freezes the ruleset that "
                        f"grades the repo, and a bare '>=' leaves it unbounded. "
                        f"Cap it at the 1.0.0 major boundary so a lockfile "
                        f"refresh picks up each release below it: "
                        f"'{canonical}'. A floor is optional — an existing "
                        f"two-sided range such as '>=0.17.0,<1.0.0' floats too "
                        f"and needs no change."
                    ),
                    suppressions=suppressions,
                )
            ]

    # ── Branch 4: declared and floating, but not in a readable lock ─────────
    if (
        _lock_is_readable(root)
        and _locked_package_version(root, CONFORMANCE_PACKAGE) is None
    ):
        return [
            _make_finding(
                rule_id=RULE_D011,
                file=rel_pyproject,
                line=entries[0].line,
                column=entries[0].column,
                message=(
                    f"'{CONFORMANCE_PACKAGE}' is declared in "
                    f"[{entries[0].array_path}] but has no entry in uv.lock. "
                    f"CI installs from the lock, so the console script the "
                    f"remediation loop calls is still absent there. Run "
                    f"'uv lock' and commit the result alongside the "
                    f"pyproject.toml change."
                ),
                suppressions=suppressions,
            )
        ]

    return []
