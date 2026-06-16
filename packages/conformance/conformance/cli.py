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


def _ensure_gitignore_entry(root: pathlib.Path, entry: str) -> None:
    """Append *entry* to .gitignore if not already present; never overwrites."""
    gitignore = root / ".gitignore"
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if any(line.strip() == entry for line in lines):
            print(f"ok:        {gitignore}  ({entry!r} already present)")
            return
        # Append with a preceding blank line for readability.
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{entry}\n")
    else:
        gitignore.write_text(f"{entry}\n", encoding="utf-8")
    print(f"appended:  {gitignore}  ({entry!r})")


def _parse_bootstrap_args(argv: list[str]) -> dict[str, str]:
    """Parse --package-name and --unit-tests-workflow from argv.

    Supports both ``--flag value`` and ``--flag=value`` forms.
    """
    result: dict[str, str] = {
        "package_name": "app",
        "unit_tests_workflow": "tests.yaml",
    }
    _flags = {
        "--package-name": "package_name",
        "--unit-tests-workflow": "unit_tests_workflow",
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
    return result


def _cmd_bootstrap(argv: list[str]) -> int:
    """Write the SKILL.md shim and standard CI workflows into the current repo."""
    from conformance.bootstrap.render import MANAGED_WORKFLOWS, render

    kwargs = _parse_bootstrap_args(argv)
    root = pathlib.Path.cwd()

    _bootstrap_file(
        root / ".claude" / "skills" / "remediate" / "SKILL.md",
        render("remediate.md", **kwargs),
    )
    for name in MANAGED_WORKFLOWS:
        _bootstrap_file(
            root / ".github" / "workflows" / name,
            render(name, **kwargs),
        )
    _ensure_gitignore_entry(root, "remediation/")
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
  bootstrap      Write .claude/skills/remediate/SKILL.md + standard CI workflow
                 shims into .github/workflows/ (always overwrites — re-running
                 eradicates drift).
                   --package-name NAME       docstring-coverage package (default: app)
                   --unit-tests-workflow FILE build-and-publish test workflow (default: tests.yaml)
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
