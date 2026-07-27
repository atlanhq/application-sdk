"""``bootstrap`` argv parsing and its ``--help`` text.

Split out of ``conformance.bootstrap.command`` so argv parsing — flag
declaration, ``--flag value``/``--flag=value`` handling, validation, usage
text — lives separately from autodetection and the write phase.
"""

from __future__ import annotations

import sys

from conformance.bootstrap.extract import APT_PACKAGE_RE

FLAGS = {
    "--package-name": "package_name",
    "--unit-tests-workflow": "unit_tests_workflow",
    "--app-name": "app_name",
    "--app-image-name": "app_image_name",
    "--enable-e2e": "enable_e2e",
    "--services-script": "services_script",
    "--system-deps": "system_deps",
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
        "system_deps": "",
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

    result["system_deps"] = normalize_system_deps(result["system_deps"])

    return result


def normalize_system_deps(value: str) -> str:
    """Return *value* as a single space-separated apt package list.

    Splits on any whitespace (so a caller may quote a multi-line value),
    validates every token against ``APT_PACKAGE_RE`` — the same pattern the
    extraction side drops non-matching tokens by, so the flag and the
    read-back-off-disk path agree on what a package name is — and rejoins with single
    spaces so the rendered workflow is byte-identical no matter how the flag
    was spelled — which matters because C002 compares the rendered canonical
    against the on-disk file, and a value that round-trips differently would
    read as permanent drift.

    Exits 2 on a token that isn't a plausible apt package name.
    """
    tokens = value.split()
    for token in tokens:
        if not APT_PACKAGE_RE.match(token):
            print(
                f"error: --system-deps got invalid package name {token!r}"
                " (expected apt package names, e.g. 'libkrb5-dev gcc python3-dev')",
                file=sys.stderr,
            )
            sys.exit(2)
    return " ".join(tokens)


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
  --system-deps "PKG..."
                              apt packages CI installs before any `uv sync` — for a
                              dependency with no manylinux wheel that needs build
                              headers (e.g. "libkrb5-dev gcc python3-dev" for
                              pykerberos). Rendered inline into checks.yml (pre-commit)
                              and written to .github/ci-system-deps.txt, which the
                              vendored run-conformance-detect action reads before the
                              D-series resolved-env sync. Omit to auto-detect from an
                              existing checks.yml (else that file), so a bare re-run
                              preserves both instead of deleting the step — checks.yml
                              is always-overwrite. To drop it, delete the step from
                              checks.yml and the txt file, then re-run.
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
