"""``bootstrap`` argv parsing and its ``--help`` text.

Split out of ``conformance.bootstrap.command`` so argv parsing — flag
declaration, ``--flag value``/``--flag=value`` handling, validation, usage
text — lives separately from autodetection and the write phase.
"""

from __future__ import annotations

import sys

FLAGS = {
    "--package-name": "package_name",
    "--unit-tests-workflow": "unit_tests_workflow",
    "--app-name": "app_name",
    "--app-image-name": "app_image_name",
    "--enable-e2e": "enable_e2e",
    "--services-script": "services_script",
    "--enforce": "enforce",
}


def parse_bootstrap_args(argv: list[str]) -> dict[str, str]:
    """Parse bootstrap flags from argv.

    Supports both ``--flag value`` and ``--flag=value`` forms. See
    ``BOOTSTRAP_USAGE`` below for the authoritative flag documentation —
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
        for flag, dest in FLAGS.items():
            if arg == flag and i + 1 < len(argv):
                result[dest] = argv[i + 1]
                i += 1
                consumed = True
                break
            if arg.startswith(f"{flag}="):
                result[dest] = arg[len(flag) + 1 :]
                consumed = True
                break
        if not consumed and arg in FLAGS:
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


BOOTSTRAP_USAGE = """\
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
