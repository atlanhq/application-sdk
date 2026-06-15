"""Entry point for the atlan-application-sdk-conformance CLI."""

from __future__ import annotations

import sys


def _cmd_detect(argv: list[str]) -> int:
    from conformance.suite.runner import main

    return main(argv)


def _cmd_programs_dir(_argv: list[str]) -> int:
    from importlib.resources import files

    programs = files("conformance") / "programs"
    # Resolve to a real filesystem path (works for both installed wheels and
    # editable installs where the files are already on disk).
    import importlib.resources as _ir

    try:
        ctx = _ir.as_file(programs)
        with ctx as p:
            print(str(p))
    except Exception:
        # Fallback: direct path (editable installs)
        import pathlib

        here = pathlib.Path(__file__).parent
        print(str(here / "programs"))
    return 0


def _cmd_gen_rule_docs(argv: list[str]) -> int:
    from conformance.tools.generate_rule_docs import main

    # generate_rule_docs.main() parses sys.argv; splice our sub-argv in.
    sys.argv = ["atlan-application-sdk-conformance gen-rule-docs", *argv]
    try:
        main()
        return 0
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0


def _cmd_remediate(argv: list[str]) -> int:
    """Print the resolved programs path + version, then exit.

    The actual remediation loop is driven by the SKILL.md shim which reads
    the .prose.md contracts from the printed programs directory.
    """
    import pathlib

    from conformance import __version__

    here = pathlib.Path(__file__).parent
    programs = here / "programs"
    print(f"atlan-application-sdk-conformance {__version__}")
    print(f"programs: {programs}")
    print(f"entry:    {programs / 'conformance-remediation.prose.md'}")
    return 0


# The SKILL.md written by `bootstrap`. Keep it minimal and stable — the real
# logic lives in the package; this shim just locates and invokes it.
_SKILL_MD = """\
---
name: remediate
description: Drive the conformance remediation loop (validators + OpenProse programs from the atlan-application-sdk-conformance package)
argument-hint: "[--area error-handling|logging|ci] [--strict] [path]"
---

1. Resolve programs dir:
   - Inside a connector repo: `PROGRAMS=$(uv run atlan-application-sdk-conformance programs-dir)`
   - Anywhere else: `PROGRAMS=$(uvx atlan-application-sdk-conformance@latest programs-dir)`
2. Read `$PROGRAMS/conformance-remediation.prose.md` and execute it as the entry contract.
3. All gated re-checks call `atlan-application-sdk-conformance detect` — follow the .prose.md exactly.
"""


def _cmd_bootstrap(argv: list[str]) -> int:
    """Write .claude/skills/remediate/SKILL.md in the current repo (or --force to overwrite)."""
    import pathlib

    force = "--force" in argv
    dest = pathlib.Path.cwd() / ".claude" / "skills" / "remediate" / "SKILL.md"

    if dest.exists() and not force:
        print(f"already installed: {dest}  (pass --force to overwrite)")
        return 0

    existed = dest.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_SKILL_MD)
    action = "updated" if existed else "installed"
    print(f"{action}: {dest}")
    return 0


_COMMANDS = {
    "detect": _cmd_detect,
    "programs-dir": _cmd_programs_dir,
    "gen-rule-docs": _cmd_gen_rule_docs,
    "remediate": _cmd_remediate,
    "bootstrap": _cmd_bootstrap,
}

_USAGE = """\
usage: atlan-application-sdk-conformance <command> [args]

commands:
  detect         Run the conformance suite and emit SARIF
  programs-dir   Print the absolute path to the bundled .prose.md programs
  gen-rule-docs  Regenerate rule docs from Python rule definitions
  remediate      Print programs path + version banner (SKILL.md drives execution)
  bootstrap      Write ~/.claude/skills/remediate/SKILL.md  (--force to overwrite)
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
