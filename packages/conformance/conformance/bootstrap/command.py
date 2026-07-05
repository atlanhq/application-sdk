"""``bootstrap`` command: write the SKILL.md shim and standard CI workflows.

Split out of ``conformance.cli`` so that module stays a thin dispatcher (every
other subcommand there is a 3-6 line delegator into its own module) — this
was by far the largest and most actively-changed subcommand. Argv parsing
lives in ``conformance.bootstrap.args`` and flag autodetection lives in
``conformance.bootstrap.autodetect``; this module is the orchestrator plus
the actual write-phase helpers (``_bootstrap_file``, the self-guard, and the
``_sync_*`` functions for the write-if-absent scaffolds).
"""

from __future__ import annotations

import json
import pathlib
import tomllib

from conformance.bootstrap.args import BOOTSTRAP_USAGE, parse_bootstrap_args
from conformance.bootstrap.autodetect import apply_bootstrap_autodetection
from conformance.bootstrap.render import MANAGED_ACTION_FILES, MANAGED_WORKFLOWS, render

_PACKAGE_NAME = "atlan-application-sdk-conformance"

# Statuses that count as "this path was written" for --json's touched_files
# manifest (see main()). Everything else (an "exists"/"unchanged" no-op) is
# reported under `unchanged` instead.
_TOUCHED_STATUSES = frozenset({"installed", "updated", "scaffolded", "backed_up"})


def _bootstrap_file(dest: pathlib.Path, content: str) -> str:
    """Write *content* to *dest*, creating parent directories as needed.

    Always-overwrite-managed — bootstrap owns these files and re-running is
    how drift is eradicated — but a no-op write when *content* already
    matches what's on disk prints ``ok (up to date)`` instead of ``updated``.
    This matters beyond cosmetics: ``touched_files`` (see
    ``remediate-finding.prose.md``) is derived from which paths print an
    ``installed:``/``updated:``/``backed up:`` prefix here, and only an
    actually-changed path should count as touched by a given remediation
    pass — an unconditional ``updated:`` on every re-run would make every
    bootstrap-based fix report all managed files as touched, not just the
    one(s) that actually drifted.

    Returns ``"installed"``, ``"updated"``, or ``"unchanged"`` — the same
    classification the printed prefix encodes, but structured for ``main()``
    to fold into the ``--json`` touched-files manifest without a caller
    having to re-derive it from stdout text.
    """
    if dest.exists():
        try:
            unchanged = dest.read_text(encoding="utf-8") == content
        except (OSError, UnicodeDecodeError):
            unchanged = False
        if unchanged:
            print(f"ok (up to date): {dest}")
            return "unchanged"
        dest.write_text(content, encoding="utf-8")
        print(f"updated: {dest}")
        return "updated"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(f"installed: {dest}")
    return "installed"


def _is_inside_conformance_repo(start: pathlib.Path) -> bool:
    """Detect whether *start* is anywhere inside the atlan-application-sdk-conformance
    package's own source checkout.

    Walks upward from *start* (rather than checking *start* itself) so the
    detection holds regardless of which subdirectory bootstrap is invoked
    from — the repo root, inside packages/conformance/ itself, or any other
    subdirectory in between.

    Keyed on ``packages/conformance/pyproject.toml`` naming this exact
    package (``atlan-application-sdk-conformance``), not merely on a
    directory named ``packages/conformance`` existing — a bare directory-name
    check would silently no-op the entire bootstrap write phase (exit 0, no
    scaffolding installed) in any consumer monorepo that happens to contain
    an unrelated ``packages/conformance/`` path.
    """
    for candidate in (start, *start.parents):
        pyproject = candidate / "packages" / "conformance" / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == _PACKAGE_NAME:
            return True
    return False


def _sync_tests_yaml(
    root: pathlib.Path, kwargs: dict[str, str]
) -> list[tuple[pathlib.Path, str]]:
    """tests.yaml — write-if-absent scaffold; apps customise freely.

    C002 tracks drift at WARN only. Delete + re-run to force-regenerate.

    Returns the ``(path, status)`` pairs this call wrote or left alone, for
    ``main()``'s ``--json`` touched-files manifest.
    """
    tests_dest = root / ".github" / "workflows" / "tests.yaml"
    if not tests_dest.exists():
        tests_dest.parent.mkdir(parents=True, exist_ok=True)
        tests_dest.write_text(render("tests.yaml", **kwargs), encoding="utf-8")
        print(f"scaffolded: {tests_dest}")
        return [(tests_dest, "scaffolded")]
    print(f"ok (exists): {tests_dest}  (edit freely; C002 tracks drift at WARN)")
    return [(tests_dest, "exists")]


def _sync_renovate_json(
    root: pathlib.Path, kwargs: dict[str, str], force_renovate: bool
) -> list[tuple[pathlib.Path, str]]:
    """renovate.json — write-if-absent normally; force-overwrite when
    ``--enforce`` is passed explicitly so re-running with ``--enforce true``
    upgrades a soft-mode repo without needing to delete the file first.

    Returns the ``(path, status)`` pairs this call wrote or left alone —
    possibly two entries (the ``.bak`` backup plus the updated file itself)
    when a customised ``renovate.json`` is force-overwritten.
    """
    renovate_dest = root / "renovate.json"
    if not renovate_dest.exists():
        renovate_dest.write_text(render("renovate.json", **kwargs), encoding="utf-8")
        print(f"scaffolded: {renovate_dest}")
        return [(renovate_dest, "scaffolded")]
    if force_renovate:
        existing = renovate_dest.read_text(encoding="utf-8")
        target = render("renovate.json", **kwargs)
        if existing == target:
            print(f"ok (up to date): {renovate_dest}")
            return [(renovate_dest, "unchanged")]
        results: list[tuple[pathlib.Path, str]] = []
        canonical_hard = render("renovate.json", automerge="true")
        canonical_soft = render("renovate.json", automerge="false")
        if existing not in (canonical_hard, canonical_soft):
            bak = renovate_dest.with_suffix(".json.bak")
            bak.write_text(existing, encoding="utf-8")
            print(f"backed up: {bak}  (had custom content; review before committing)")
            results.append((bak, "backed_up"))
        renovate_dest.write_text(target, encoding="utf-8")
        print(f"updated: {renovate_dest}")
        results.append((renovate_dest, "updated"))
        return results
    print(
        f"ok (exists): {renovate_dest}"
        "  (edit freely; pass --enforce to update enforcement mode)"
    )
    return [(renovate_dest, "exists")]


def _sync_gitignore(root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """.gitignore — write-if-absent scaffold. C003 warns about missing entries."""
    gitignore_dest = root / ".gitignore"
    if not gitignore_dest.exists():
        gitignore_dest.write_text(render(".gitignore"), encoding="utf-8")
        print(f"scaffolded: {gitignore_dest}")
        return [(gitignore_dest, "scaffolded")]
    print(
        f"ok (exists): {gitignore_dest}  (edit freely; C003 warns on missing entries)"
    )
    return [(gitignore_dest, "exists")]


def _sync_contract_ledger(root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """contract_schema.lock.json — write-if-absent scaffold.

    B006 (StaleContractLedger) is a hard FAIL-tier rule active from day one:
    with no ledger present, the ledger-absent fallback loads the SDK's own
    bundled ledger, which has none of the app's fields recorded, so any app
    with existing entrypoint contract fields fails enforced mode on its very
    first run. Seed the baseline from current source — same output as
    running ``gen-contract-ledger`` by hand.
    """
    ledger_dest = root / "contract_schema.lock.json"
    if not ledger_dest.exists():
        from conformance.suite.checks.deprecation._ledger_schema import (
            load_ledger,
            serialize,
        )
        from conformance.tools.generate_contract_ledger import build_ledger

        ledger = build_ledger(root, load_ledger(None))
        ledger_dest.write_text(serialize(ledger), encoding="utf-8")
        print(f"scaffolded: {ledger_dest} ({len(ledger.fields)} fields)")
        return [(ledger_dest, "scaffolded")]
    print(
        f"ok (exists): {ledger_dest}"
        "  (run `gen-contract-ledger` to refresh; B005/B006 track drift)"
    )
    return [(ledger_dest, "exists")]


def main(argv: list[str]) -> int:
    """Write the SKILL.md shim and standard CI workflows into the current repo."""
    if "-h" in argv or "--help" in argv:
        print(BOOTSTRAP_USAGE)
        return 0

    # --json is a plain output-mode toggle (like -h/--help), not a --flag
    # value pair, so it's stripped before parse_bootstrap_args ever sees it
    # rather than being added to args.FLAGS.
    emit_json = "--json" in argv
    if emit_json:
        argv = [a for a in argv if a != "--json"]

    kwargs = parse_bootstrap_args(argv)
    root = pathlib.Path.cwd()

    # bootstrap scaffolds a *consumer app* repo. Every file it would write —
    # SKILL.md, the managed workflow/action shims, tests.yaml, renovate.json,
    # .gitignore, contract_schema.lock.json — is either hand-maintained here
    # or simply doesn't apply to a library repo. No-op the entire write phase
    # rather than special-casing each managed file individually — a per-file
    # guard silently stops covering new managed files the moment one is added
    # without updating it (this replaced an earlier guard that covered only
    # SKILL.md and missed MANAGED_WORKFLOWS/MANAGED_ACTION_FILES, which are
    # just as hand-authored in this repo).
    if _is_inside_conformance_repo(root):
        print(
            "skipped: bootstrap is a no-op inside the"
            " atlan-application-sdk-conformance repo itself"
            " (its .github/, SKILL.md, tests.yaml, and renovate.json are"
            " hand-maintained, not bootstrap-managed)"
        )
        if emit_json:
            print(json.dumps({"skipped": True, "touched": [], "unchanged": []}))
        return 0

    # force_renovate must reflect only an *explicit* --enforce on this
    # invocation, captured before autodetection fills kwargs["enforce"] in
    # from an existing conformance.yaml -- renovate.json stays write-if-absent
    # on a bare re-run even though conformance.yaml's enforcement mode is now
    # auto-detected.
    force_renovate = bool(kwargs["enforce"])
    apply_bootstrap_autodetection(kwargs, root)

    # Derive the two render variables from --enforce (explicit or detected).
    # enforce="" (never set, nothing to detect) → hard defaults.
    # enforce="false" → soft/observe mode.
    # enforce="true"  → hard mode.
    enforce = kwargs.pop("enforce")
    kwargs["exit_zero"] = "true" if enforce == "false" else "false"
    kwargs["automerge"] = "false" if enforce == "false" else "true"

    # Structured record of every path this invocation touched or left
    # unchanged, for --json below. Populated alongside (never instead of) the
    # human-readable prints above and in the sync helpers, so a caller that
    # needs touched_files for a revert-scope decision (see
    # remediate-finding.prose.md) doesn't have to re-derive it by pattern
    # matching this command's prose stdout. Reported repo-relative to match
    # touched_files' documented convention (remediate-finding.prose.md) and
    # the K003/K004 `git status --porcelain`-derived case, which are both
    # naturally repo-relative already.
    touched: list[str] = []
    unchanged: list[str] = []

    def _record(path: pathlib.Path, status: str) -> None:
        rel = str(path.relative_to(root))
        (touched if status in _TOUCHED_STATUSES else unchanged).append(rel)

    skill_md = root / ".claude" / "skills" / "remediate" / "SKILL.md"
    _record(skill_md, _bootstrap_file(skill_md, render("remediate.md", **kwargs)))
    for name in MANAGED_WORKFLOWS:
        dest = root / ".github" / "workflows" / name
        _record(dest, _bootstrap_file(dest, render(name, **kwargs)))

    # Non-workflow files referenced by conformance-reusable.yaml via a local
    # `./...`-relative path, which GitHub resolves against the caller's
    # checkout — every consumer repo needs its own copy or the C/D-series
    # (and any other series whose paths filter matches) legs fail with
    # "Can't find action.yml". Static, always-overwrite like MANAGED_WORKFLOWS.
    for dest_rel, template_name in MANAGED_ACTION_FILES:
        dest = root / dest_rel
        _record(dest, _bootstrap_file(dest, render(template_name)))

    for path, status in _sync_tests_yaml(root, kwargs):
        _record(path, status)
    for path, status in _sync_renovate_json(root, kwargs, force_renovate):
        _record(path, status)
    for path, status in _sync_gitignore(root):
        _record(path, status)
    for path, status in _sync_contract_ledger(root):
        _record(path, status)

    if emit_json:
        print(
            json.dumps({"skipped": False, "touched": touched, "unchanged": unchanged})
        )

    return 0
