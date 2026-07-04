"""``bootstrap`` command: write the SKILL.md shim and standard CI workflows.

Split out of ``conformance.cli`` so that module stays a thin dispatcher (every
other subcommand there is a 3-6 line delegator into its own module) — this is
by far the largest and most actively-changed subcommand.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tomllib

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_field,
    extract_renovate_automerge,
)

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
        except OSError:
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


def _read_workflow_field(path: pathlib.Path, field: str) -> str:
    """Return the value of ``field: <value>`` in *path*, or ``""``.

    Delegates to ``conformance.bootstrap.extract``'s ``extract_field`` (the
    single source of truth also used by the C002 checker's "read a rendered
    param back off disk" extraction) so this autodetection path and C002's
    drift-comparison extraction can never silently diverge on how a rendered
    field is parsed.
    """
    if not path.exists():
        return ""
    try:
        return extract_field(path.read_text(encoding="utf-8"), field)
    except OSError:
        return ""


def _read_atlan_yaml_name(root: pathlib.Path) -> str:
    """Return the ``name:`` value from ``atlan.yaml`` in *root*, or ``""``."""
    return _read_workflow_field(root / "atlan.yaml", "name")


def _derive_app_name_from_dir(root: pathlib.Path) -> str:
    """Derive app name from the repo directory name.

    Strips a leading ``atlan-`` prefix and a trailing ``-app`` suffix so that
    e.g. ``atlan-openapi-app`` → ``openapi``.  Falls back to ``"app"`` if the
    result would be empty.
    """
    name = root.name
    if name.startswith("atlan-"):
        name = name[len("atlan-") :]
    if name.endswith("-app"):
        name = name[: -len("-app")]
    return name or "app"


def _read_enforce_from_renovate(root: pathlib.Path) -> str:
    """Fall back to *root*'s on-disk ``renovate.json`` enforcement signal.

    Used only when ``conformance.yaml``'s ``exit-zero`` line can't be read
    (see ``_read_conformance_enforce``) — ``renovate.json``'s own
    ``lockFileMaintenance`` block (soft mode) or absence thereof (hard mode)
    is a second, independent signal of the repo's actual enforcement mode,
    read via ``extract_renovate_automerge`` (the same structural check the
    C002 checker uses), so autodetection doesn't have to guess.
    """
    renovate = root / "renovate.json"
    if not renovate.exists():
        return ""
    try:
        automerge = extract_renovate_automerge(renovate.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return "false" if automerge == "false" else "true"


def _read_conformance_enforce(path: pathlib.Path, root: pathlib.Path) -> str:
    """Return the ``--enforce`` value that reproduces this repo's existing
    enforcement mode, or ``""`` if there is truly nothing to detect.

    Primary signal is *path*'s (``conformance.yaml``) ``exit-zero`` line. The
    rendered line is ``exit-zero: ${{ ... || << exit_zero >> }}`` — the
    boolean is the last token before the closing ``}}``, not the first token
    after ``exit-zero:`` (unlike the other managed-file fields), so this
    can't reuse ``_read_workflow_field``. ``exit-zero: true`` is soft/observe
    mode (``--enforce false``); ``exit-zero: false`` is hard-gate
    (``--enforce true``).

    Reuses ``EXIT_ZERO_RE`` from ``conformance.bootstrap.extract`` as the
    single source of truth for the pattern, rather than re-declaring an
    identical regex here.

    If *path* is absent, there is nothing to detect and this returns ``""``
    (falls through to the hard-gate default at derivation time) — that's the
    normal first-bootstrap case. But if *path* exists and its ``exit-zero``
    line simply doesn't match the expected pattern (hand-edited, or rendered
    by an older bootstrap template), silently falling through to the same
    hard-gate default would flip an intentionally soft-mode repo to hard-gate
    on a bare re-run — while ``renovate.json`` (which a bare re-run never
    force-overwrites) stays in its original soft-mode content, leaving the
    two managed files in different enforcement modes. Fall back to
    ``renovate.json``'s own on-disk signal instead of guessing hard-gate.
    """
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = EXIT_ZERO_RE.search(line)
            if m:
                return "false" if m.group(1) == "true" else "true"
    except OSError:
        return ""
    print(
        f"warning: {path} exists but its exit-zero line is unparseable"
        " -- falling back to renovate.json's enforcement signal"
    )
    return _read_enforce_from_renovate(root)


def _apply_bootstrap_autodetection(kwargs: dict[str, str], root: pathlib.Path) -> None:
    """Fill in any bootstrap flag left unset (``""``) with its auto-detected value.

    Each flag's auto-detection reads back an existing managed file so that
    re-running ``bootstrap`` with no explicit flags reuses a repo's current
    customization instead of resetting it to the hardcoded default.
    """
    # package-name: existing docstring-coverage.yaml, else "app".
    if not kwargs["package_name"]:
        kwargs["package_name"] = (
            _read_workflow_field(
                root / ".github" / "workflows" / "docstring-coverage.yaml",
                "package_name",
            )
            or "app"
        )
    # unit-tests-workflow: existing build-and-publish.yaml, else "tests.yaml".
    if not kwargs["unit_tests_workflow"]:
        kwargs["unit_tests_workflow"] = (
            _read_workflow_field(
                root / ".github" / "workflows" / "build-and-publish.yaml",
                "unit_tests_workflow_file",
            )
            or "tests.yaml"
        )
    # app-name: atlan.yaml `name:` field, else the repo directory name.
    if not kwargs["app_name"]:
        kwargs["app_name"] = _read_atlan_yaml_name(root) or _derive_app_name_from_dir(
            root
        )
    # services-script: existing .github/test/setup-services.sh, else unset.
    if not kwargs["services_script"]:
        candidate = root / ".github" / "test" / "setup-services.sh"
        if candidate.exists():
            kwargs["services_script"] = ".github/test/setup-services.sh"
    # enforce: existing conformance.yaml's exit-zero mode, else unset (falls
    # through to the hard-gate default at derivation time). Deliberately does
    # NOT affect force_renovate in main() -- renovate.json is only
    # force-overwritten when --enforce was passed explicitly on this
    # invocation, not when it was merely auto-detected.
    if not kwargs["enforce"]:
        kwargs["enforce"] = _read_conformance_enforce(
            root / ".github" / "workflows" / "conformance.yaml", root
        )


_FLAGS = {
    "--package-name": "package_name",
    "--unit-tests-workflow": "unit_tests_workflow",
    "--app-name": "app_name",
    "--app-image-name": "app_image_name",
    "--enable-e2e": "enable_e2e",
    "--services-script": "services_script",
    "--enforce": "enforce",
}


def _parse_bootstrap_args(argv: list[str]) -> dict[str, str]:
    """Parse bootstrap flags from argv.

    Supports both ``--flag value`` and ``--flag=value`` forms. See
    ``_BOOTSTRAP_USAGE`` below for the authoritative flag documentation —
    kept in one place so it can't drift out of sync with this parser.
    """
    result: dict[str, str] = {
        "package_name": "",
        "unit_tests_workflow": "",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
        "enforce": "",  # "" = not explicitly set; "true"/"false" = explicit
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        consumed = False
        for flag, dest in _FLAGS.items():
            if arg == flag and i + 1 < len(argv):
                result[dest] = argv[i + 1]
                i += 1
                consumed = True
                break
            if arg.startswith(f"{flag}="):
                result[dest] = arg[len(flag) + 1 :]
                consumed = True
                break
        if not consumed and arg in _FLAGS:
            print(f"error: option {arg!r} requires a value", file=sys.stderr)
            sys.exit(2)
        if not consumed and arg.startswith("-") and arg not in ("-h", "--help"):
            print(f"error: unknown option {arg!r}", file=sys.stderr)
            sys.exit(2)
        i += 1

    if result["enable_e2e"] not in ("true", "false"):
        print(
            f"error: --enable-e2e must be 'true' or 'false', got {result['enable_e2e']!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    if result["enforce"] not in ("", "true", "false"):
        print(
            f"error: --enforce must be 'true' or 'false', got {result['enforce']!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    return result


_BOOTSTRAP_USAGE = """\
usage: atlan-application-sdk-conformance bootstrap [options]

Write .claude/skills/remediate/SKILL.md + all standard CI workflow shims into
.github/workflows/, plus the vendored .github/actions/run-conformance-detect/action.yaml
and .github/scripts/build_conformance_args.py that conformance-reusable.yaml needs on
disk in every caller repo. All of these always overwrite (re-running eradicates drift).
tests.yaml, renovate.json, and contract_schema.lock.json are write-if-absent by default;
pass --enforce true|false to also update renovate.json's enforcement mode.

options:
  --package-name NAME         docstring-coverage package; omit to auto-detect from an
                              existing docstring-coverage.yaml (else "app")
  --unit-tests-workflow FILE  build-and-publish test workflow; omit to auto-detect from
                              an existing build-and-publish.yaml (else "tests.yaml")
  --app-name NAME             connector app name for tests.yaml (default: from atlan.yaml, else "app")
  --app-image-name NAME       GHCR image name for tests.yaml (default: atlan-<app-name>-app)
  --enable-e2e true|false     enable e2e in tests.yaml (default: true, line omitted)
  --services-script PATH      services setup script (default: auto-detected from .github/test/setup-services.sh)
  --enforce true|false        enforcement mode; omit to auto-detect from an existing
                              conformance.yaml (else hard-gate). Pass explicitly (either
                              value) to also force-update renovate.json.
                              true  — hard gate: conformance blocks on violations,
                                      Renovate auto-merges when CI is green.
                              false — soft/observe: conformance tracks without blocking,
                                      Renovate raises PRs but humans must merge.
  --json                       after the normal output, print one final JSON line:
                              {"skipped": bool, "touched": [...], "unchanged": [...]}.
                              `touched` lists every path this invocation actually wrote
                              (scaffolded/installed/updated/backed-up); use it as the
                              structured, non-prose source for touched_files instead of
                              pattern-matching the prefixed stdout lines above.
  -h, --help                  show this help message and exit
"""


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
    from conformance.bootstrap.render import render

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
    from conformance.bootstrap.render import render

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
    from conformance.bootstrap.render import render

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
        print(_BOOTSTRAP_USAGE)
        return 0

    # --json is a plain output-mode toggle (like -h/--help), not a --flag
    # value pair, so it's stripped before _parse_bootstrap_args ever sees it
    # rather than being added to _FLAGS.
    emit_json = "--json" in argv
    if emit_json:
        argv = [a for a in argv if a != "--json"]

    from conformance.bootstrap.render import (
        MANAGED_ACTION_FILES,
        MANAGED_WORKFLOWS,
        render,
    )

    kwargs = _parse_bootstrap_args(argv)
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
    _apply_bootstrap_autodetection(kwargs, root)

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
    # matching this command's prose stdout.
    touched: list[str] = []
    unchanged: list[str] = []

    def _record(path: pathlib.Path, status: str) -> None:
        (touched if status in _TOUCHED_STATUSES else unchanged).append(str(path))

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
