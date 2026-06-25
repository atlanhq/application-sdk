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
    """
    result: dict[str, str] = {
        "package_name": "app",
        "unit_tests_workflow": "tests.yaml",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
    }
    _flags = {
        "--package-name": "package_name",
        "--unit-tests-workflow": "unit_tests_workflow",
        "--app-name": "app_name",
        "--app-image-name": "app_image_name",
        "--enable-e2e": "enable_e2e",
        "--services-script": "services_script",
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

    return result


def _cmd_bootstrap(argv: list[str]) -> int:
    """Write the SKILL.md shim and standard CI workflows into the current repo."""
    from conformance.bootstrap.render import MANAGED_WORKFLOWS, render

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

    _bootstrap_file(
        root / ".claude" / "skills" / "remediate" / "SKILL.md",
        render("remediate.md", **kwargs),
    )
    for name in MANAGED_WORKFLOWS:
        _bootstrap_file(
            root / ".github" / "workflows" / name,
            render(name, **kwargs),
        )

    # Write-if-absent scaffolds — created once; apps customise freely.
    # C002 tracks drift at WARN only.  Delete + re-run to force-regenerate.
    for scaffold_dest, scaffold_name in [
        (root / ".github" / "workflows" / "tests.yaml", "tests.yaml"),
        (root / "renovate.json", "renovate.json"),
    ]:
        if not scaffold_dest.exists():
            scaffold_dest.parent.mkdir(parents=True, exist_ok=True)
            scaffold_dest.write_text(render(scaffold_name, **kwargs), encoding="utf-8")
            print(f"scaffolded: {scaffold_dest}")
        else:
            print(
                f"ok (exists): {scaffold_dest}  (edit freely; C002 tracks drift at WARN)"
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
                       --outfile PATH  ledger path (default: package data)
                       --check       verify ledger is current; exit 1 if stale
  ledger-guard         CI append-only guard: block ledger deletions and type changes between
                       base ref and HEAD (run after fetch-depth: 0 checkout)
                         --base-ref REF      git ref for the base (default: origin/main)
                         --ledger-path PATH  repo-relative path to the ledger file
  remediate      Print programs path + version banner (SKILL.md drives execution)
  bootstrap      Write .claude/skills/remediate/SKILL.md + all standard CI workflow
                 shims into .github/workflows/. The 14 managed shims always overwrite
                 (re-running eradicates drift). tests.yaml is write-if-absent
                 (scaffolded once; delete it and re-run to regenerate from canonical).
                   --package-name NAME         docstring-coverage package (default: app)
                   --unit-tests-workflow FILE   build-and-publish test workflow (default: tests.yaml)
                   --app-name NAME             connector app name for tests.yaml (default: from atlan.yaml, else "app")
                   --app-image-name NAME       GHCR image name for tests.yaml (default: atlan-<app-name>-app)
                   --enable-e2e true|false     enable e2e in tests.yaml (default: true, line omitted)
                   --services-script PATH      services setup script (default: auto-detected from .github/test/setup-services.sh)
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
