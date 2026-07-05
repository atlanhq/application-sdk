"""Entry point for the atlan-application-sdk-conformance CLI."""

from __future__ import annotations

import pathlib
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


def _cmd_bootstrap(argv: list[str]) -> int:
    from conformance.bootstrap.command import main

    return main(argv)


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
                 (re-running eradicates drift). tests.yaml, renovate.json, and
                 contract_schema.lock.json are write-if-absent by default.
                 Run `bootstrap --help` for the full option list.
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
