"""Entry point for the atlan-application-sdk-conformance CLI."""

from __future__ import annotations

import pathlib
import re
import sys


def _cmd_detect(argv: list[str]) -> int:
    from conformance.suite.runner import main

    return main(argv)


def _cmd_programs_dir(_argv: list[str]) -> int:
    import importlib.resources as _ir

    programs = _ir.files("conformance") / "programs"
    # Resolve to a real filesystem path (works for both installed wheels and
    # editable installs where the files are already on disk).
    try:
        ctx = _ir.as_file(programs)
        with ctx as p:
            print(str(p))
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback: direct path (editable installs)
        here = pathlib.Path(__file__).parent
        print(str(here / "programs"))
    return 0


def _cmd_gen_rule_docs(argv: list[str]) -> int:
    from conformance.tools.generate_rule_docs import main

    try:
        main(argv)
        return 0
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0


def _cmd_gen_deprecations(argv: list[str]) -> int:
    from conformance.tools.generate_deprecations import main

    try:
        main(argv)
        return 0
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0


def _cmd_ledger_guard(argv: list[str]) -> int:
    from conformance.tools.ledger_guard import main

    return main(argv)


def _cmd_gen_contract_ledger(argv: list[str]) -> int:
    from conformance.tools.generate_contract_ledger import main

    try:
        main(argv)
        return 0
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0


def _cmd_remediate(argv: list[str]) -> int:
    """Print the resolved programs path + version, then exit.

    The actual remediation loop is driven by the SKILL.md shim which reads
    the .prose.md contracts from the printed programs directory.
    """
    from conformance import __version__

    here = pathlib.Path(__file__).parent
    programs = here / "programs"
    print(f"atlan-application-sdk-conformance {__version__}")
    print(f"programs: {programs}")
    print(f"entry:    {programs / 'conformance-remediation.prose.md'}")
    return 0


def _bootstrap_file(dest: pathlib.Path, content: str) -> None:
    """Write *content* to *dest*, creating parent directories as needed.

    Always overwrites — bootstrap owns these files and re-running is how
    drift is eradicated.
    """
    existed = dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(f"{'updated' if existed else 'installed'}: {dest}")


def _read_atlan_yaml_name(root: pathlib.Path) -> str:
    """Return the ``name:`` value from ``atlan.yaml`` in *root*, or ``""``."""

    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.exists():
        return ""
    try:
        for line in atlan_yaml.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^name:\s+(\S+)", line)
            if m:
                return m.group(1).strip("\"'")
    except OSError:
        pass
    return ""


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


def _parse_bootstrap_args(argv: list[str]) -> dict[str, str]:
    """Parse bootstrap flags from argv.

    Supports both ``--flag value`` and ``--flag=value`` forms.

    Flags
    -----
    --package-name NAME          docstring-coverage package (default: app)
    --unit-tests-workflow FILE   build-and-publish test workflow (default: tests.yaml)
    --app-name NAME              connector app name for tests.yaml scaffold
                                 (default: auto-detected from atlan.yaml, else "app")
    --app-image-name NAME        GHCR image name for tests.yaml scaffold (default: atlan-<app-name>-app)
    --enable-e2e BOOL            enable e2e job in tests.yaml scaffold (default: true)
    --services-script PATH       path to services setup script for tests.yaml scaffold
                                 (default: auto-detected from .github/test/setup-services.sh)
    --enforce BOOL               enforcement mode; omit for hard-gate defaults without
                                 force-updating renovate.json. Pass explicitly (either
                                 value) to also force-update renovate.json.
                                 true  — hard gate: conformance blocks on violations,
                                         Renovate auto-merges when CI is green.
                                 false — soft/observe mode: conformance tracks violations
                                         without blocking, Renovate raises PRs but humans
                                         must merge.
    """
    result: dict[str, str] = {
        "package_name": "app",
        "unit_tests_workflow": "tests.yaml",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
        "enforce": "",  # "" = not explicitly set; "true"/"false" = explicit
    }
    _flags = {
        "--package-name": "package_name",
        "--unit-tests-workflow": "unit_tests_workflow",
        "--app-name": "app_name",
        "--app-image-name": "app_image_name",
        "--enable-e2e": "enable_e2e",
        "--services-script": "services_script",
        "--enforce": "enforce",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        for flag, dest in _flags.items():
            if arg == flag and i + 1 < len(argv):
                result[dest] = argv[i + 1]
                i += 1
                break
            if arg.startswith(f"{flag}="):
                result[dest] = arg[len(flag) + 1 :]
                break
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


def _cmd_bootstrap(argv: list[str]) -> int:
    """Write the SKILL.md shim and standard CI workflows into the current repo."""
    from conformance.bootstrap.render import (
        MANAGED_ACTION_FILES,
        MANAGED_WORKFLOWS,
        render,
    )

    kwargs = _parse_bootstrap_args(argv)
    root = pathlib.Path.cwd()
    # Auto-detect app name when --app-name was not supplied:
    # 1. atlan.yaml `name:` field, 2. repo directory name, 3. "app".
    if not kwargs["app_name"]:
        kwargs["app_name"] = _read_atlan_yaml_name(root) or _derive_app_name_from_dir(
            root
        )
    # Auto-detect services-script when --services-script was not supplied.
    if not kwargs["services_script"]:
        candidate = root / ".github" / "test" / "setup-services.sh"
        if candidate.exists():
            kwargs["services_script"] = ".github/test/setup-services.sh"

    # Derive the two render variables from --enforce.
    # enforce="" (not set) → hard defaults but no force-overwrite of scaffolds.
    # enforce="false"      → soft/observe mode + force-overwrite renovate.json.
    # enforce="true"       → hard mode + force-overwrite renovate.json.
    enforce = kwargs.pop("enforce")
    kwargs["exit_zero"] = "true" if enforce == "false" else "false"
    kwargs["automerge"] = "false" if enforce == "false" else "true"
    force_renovate = enforce in ("true", "false")

    _bootstrap_file(
        root / ".claude" / "skills" / "remediate" / "SKILL.md",
        render("remediate.md", **kwargs),
    )
    for name in MANAGED_WORKFLOWS:
        _bootstrap_file(
            root / ".github" / "workflows" / name,
            render(name, **kwargs),
        )

    # Non-workflow files referenced by conformance-reusable.yaml via a local
    # `./...`-relative path, which GitHub resolves against the caller's
    # checkout — every consumer repo needs its own copy or the C/D-series
    # (and any other series whose paths filter matches) legs fail with
    # "Can't find action.yml". Static, always-overwrite like MANAGED_WORKFLOWS.
    for dest_rel, template_name in MANAGED_ACTION_FILES:
        _bootstrap_file(root / dest_rel, render(template_name))

    # tests.yaml — write-if-absent scaffold; apps customise freely.
    # C002 tracks drift at WARN only.  Delete + re-run to force-regenerate.
    tests_dest = root / ".github" / "workflows" / "tests.yaml"
    if not tests_dest.exists():
        tests_dest.parent.mkdir(parents=True, exist_ok=True)
        tests_dest.write_text(render("tests.yaml", **kwargs), encoding="utf-8")
        print(f"scaffolded: {tests_dest}")
    else:
        print(f"ok (exists): {tests_dest}  (edit freely; C002 tracks drift at WARN)")

    # renovate.json — write-if-absent normally; force-overwrite when --enforce
    # is passed explicitly so re-running with --enforce true upgrades a
    # soft-mode repo without needing to delete the file first.
    renovate_dest = root / "renovate.json"
    if not renovate_dest.exists():
        renovate_dest.write_text(render("renovate.json", **kwargs), encoding="utf-8")
        print(f"scaffolded: {renovate_dest}")
    elif force_renovate:
        existing = renovate_dest.read_text(encoding="utf-8")
        target = render("renovate.json", **kwargs)
        if existing == target:
            print(f"ok (up to date): {renovate_dest}")
        else:
            canonical_hard = render("renovate.json", automerge="true")
            canonical_soft = render("renovate.json", automerge="false")
            if existing not in (canonical_hard, canonical_soft):
                bak = renovate_dest.with_suffix(".json.bak")
                bak.write_text(existing, encoding="utf-8")
                print(
                    f"backed up: {bak}  (had custom content; review before committing)"
                )
            renovate_dest.write_text(target, encoding="utf-8")
            print(f"updated: {renovate_dest}")
    else:
        print(
            f"ok (exists): {renovate_dest}"
            "  (edit freely; pass --enforce to update enforcement mode)"
        )

    # .gitignore — write-if-absent scaffold.  C003 warns about missing entries.
    gitignore_dest = root / ".gitignore"
    if not gitignore_dest.exists():
        gitignore_dest.write_text(render(".gitignore"), encoding="utf-8")
        print(f"scaffolded: {gitignore_dest}")
    else:
        print(
            f"ok (exists): {gitignore_dest}  (edit freely; C003 warns on missing entries)"
        )

    return 0


def _cmd_renovate_scan(argv: list[str]) -> int:
    from conformance.renovate.scan import main

    return main(argv)


_COMMANDS = {
    "detect": _cmd_detect,
    "programs-dir": _cmd_programs_dir,
    "gen-rule-docs": _cmd_gen_rule_docs,
    "gen-deprecations": _cmd_gen_deprecations,
    "gen-contract-ledger": _cmd_gen_contract_ledger,
    "ledger-guard": _cmd_ledger_guard,
    "remediate": _cmd_remediate,
    "bootstrap": _cmd_bootstrap,
    "renovate-scan": _cmd_renovate_scan,
}

_USAGE = """\
usage: atlan-application-sdk-conformance <command> [args]

commands:
  detect         Run the conformance suite and emit SARIF
  programs-dir   Print the absolute path to the bundled .prose.md programs
  gen-rule-docs  Regenerate rule docs from Python rule definitions
  gen-deprecations  Regenerate the deprecated-symbol manifest from SDK source
  gen-contract-ledger  Regenerate the entrypoint-contract ledger (contract_schema.lock.json)
                       --repo DIR    repo root to scan (default: auto-detected)
                       --outfile PATH  ledger path (default: contract_schema.lock.json in cwd)
                       --check       verify ledger is current; exit 1 if stale
  ledger-guard         CI append-only guard: block ledger deletions and type changes between
                       base ref and HEAD (run after fetch-depth: 0 checkout)
                         --base-ref REF      git ref for the base (default: origin/main)
                         --ledger-path PATH  repo-relative path to the ledger file
  remediate      Print programs path + version banner (SKILL.md drives execution)
  bootstrap      Write .claude/skills/remediate/SKILL.md + all standard CI workflow
                 shims into .github/workflows/, plus the vendored
                 .github/actions/run-conformance-detect/action.yaml and
                 .github/scripts/build_conformance_args.py that conformance-reusable.yaml
                 needs on disk in every caller repo. All of these always overwrite
                 (re-running eradicates drift). tests.yaml and renovate.json are
                 write-if-absent by default; pass --enforce to update them too.
                   --package-name NAME         docstring-coverage package (default: app)
                   --unit-tests-workflow FILE   build-and-publish test workflow (default: tests.yaml)
                   --app-name NAME             connector app name for tests.yaml (default: from atlan.yaml, else "app")
                   --app-image-name NAME       GHCR image name for tests.yaml (default: atlan-<app-name>-app)
                   --enable-e2e true|false     enable e2e in tests.yaml (default: true, line omitted)
                   --services-script PATH      services setup script (default: auto-detected from .github/test/setup-services.sh)
                   --enforce true|false        enforcement mode; omit for hard-gate defaults without
                                               force-updating renovate.json. Pass explicitly (either
                                               value) to also force-update renovate.json.
                                               true  — hard gate: conformance blocks on violations,
                                                       Renovate auto-merges when CI is green.
                                               false — soft/observe: conformance tracks without blocking,
                                                       Renovate raises PRs but humans must merge.
  renovate-scan  Build Renovate fleet dashboard JSON from gh pr list output files
"""


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(_USAGE)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd not in _COMMANDS:
        print(f"error: unknown command '{cmd}'\n{_USAGE}", file=sys.stderr)
        sys.exit(1)

    sys.exit(_COMMANDS[cmd](sys.argv[2:]))
