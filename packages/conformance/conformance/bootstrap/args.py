"""``bootstrap`` argv parsing and its ``--help`` text.

Split out of ``conformance.bootstrap.command`` so argv parsing — flag
declaration, ``--flag value``/``--flag=value`` handling, validation, usage
text — lives separately from autodetection and the write phase.
"""

from __future__ import annotations

import sys

# The coverage floor is read as a module attribute rather than imported by
# value, for the same reason the C002 checker does it (see its import comment):
# a `from ... import` copy is fixed at import time, so this validation and the
# extractor that applies the floor would disagree whenever the constant is
# moved — and moving it is the only way to exercise the below-floor branch
# while the real floor is 0.
from conformance.bootstrap import extract as extract_mod
from conformance.bootstrap.extract import APT_PACKAGE_RE

FLAGS = {
    "--unit-tests-workflow": "unit_tests_workflow",
    "--app-name": "app_name",
    "--app-image-name": "app_image_name",
    "--enable-e2e": "enable_e2e",
    "--services-script": "services_script",
    "--system-deps": "system_deps",
    "--unit-coverage-fail-under": "unit_coverage_fail_under",
    "--use-ghcr-base": "use_ghcr_base",
    "--enforce": "enforce",
    "--conformance-blocking": "conformance_blocking",
    "--renovate-automerge": "renovate_automerge",
}

# Tri-state flags: "" means "not explicitly set on this invocation" (defer to
# autodetection, then to the shorthand, then to the hard default), and only
# "true"/"false" are otherwise accepted. Validated together so a new one can't
# be added to FLAGS with its validation quietly forgotten.
#
# ``--enforce`` is the shorthand; ``--conformance-blocking`` and
# ``--renovate-automerge`` are the individual 0-touch levers it expands to.
# Neither of them, and not ``--enforce`` either, has any bearing on whether
# ``tests / Tests Gate`` is a required branch-protection check — that is a
# separate lever with no prerequisite here (FND-347).
#
# ``--use-ghcr-base`` is tri-state for a different reason: it is not a 0-touch
# lever and ``--enforce`` does not expand to it, but it does need the same
# "" = defer-to-autodetection slot, because the file it writes
# (build-and-publish.yaml) is always-overwrite and a bare re-run must re-render
# an existing opt-in rather than drop it. Explicit ``false`` therefore means
# "remove the opt-in", not "leave whatever is there".
TRISTATE_FLAGS = (
    "enforce",
    "conformance_blocking",
    "renovate_automerge",
    "use_ghcr_base",
)

# Presence flags: no value, "true" when present and "false" otherwise. Declared
# here rather than stripped from argv by the caller (the way ``--json`` is)
# because these change what bootstrap *writes*, not how it reports — so the
# module that owns argv validation should be the one that rejects
# ``--resync=1``.
#
# ``--resync`` is deliberately one blanket flag rather than a per-file
# ``--resync-<name>`` or a target list: it covers everything bootstrap owns
# that a bare re-run deliberately leaves alone, and its members carry the
# same risk profile, so there is nothing for a caller to choose between. The
# two write-if-absent files it does NOT cover are excluded structurally, not
# by omission — see ``_sync_gitignore`` and ``_sync_contract_ledger``.
#
# ``--resync`` therefore implies ``--connector-review-kit``: the kit is
# centrally managed and its merges preserve per-repo content (a marked block
# in CLAUDE.md, an additive hook entry in .claude/settings.json), so it is a
# structural catch-up target like the scaffolds. The flag survives on its own
# for the repo that wants the kit *without* resyncing tests.yaml/renovate.json,
# and ``main()`` still distinguishes the two: an unmergeable review block is
# fatal when the kit was asked for by name, and skipped when it was implied.
PRESENCE_FLAGS = {
    "--resync": "resync",
    "--connector-review-kit": "connector_review_kit",
}

# Flags that used to exist, mapped to why they are gone. A caller still
# passing one gets that sentence instead of a bare "unknown option": these are
# baked into app-repo runbooks and CI steps we don't control, and "unknown
# option '--package-name'" reads as a broken install rather than as a
# deliberate retirement.
RETIRED_FLAGS = {
    "--package-name": (
        "the docstring-coverage workflow it parameterised was retired (FND-381)"
        " — bootstrap no longer writes that file and removes it on re-run"
    ),
}

_DEST_TO_FLAG = {dest: flag for flag, dest in FLAGS.items()}


def _match_retired_flag(arg: str) -> str:
    """Return the retired flag *arg* spells, in either form, else ""."""
    for flag in RETIRED_FLAGS:
        if arg == flag or arg.startswith(f"{flag}="):
            return flag
    return ""


def parse_bootstrap_args(argv: list[str]) -> dict[str, str]:
    """Parse bootstrap flags from argv.

    Supports both ``--flag value`` and ``--flag=value`` forms. See
    ``BOOTSTRAP_USAGE`` below for the authoritative flag documentation —
    kept in one place so it can't drift out of sync with this parser.
    """
    result: dict[str, str] = {
        "unit_tests_workflow": "",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
        "system_deps": "",
        "unit_coverage_fail_under": "",
        # "" = not explicitly set; "true"/"false" = explicit. See TRISTATE_FLAGS.
        "use_ghcr_base": "",
        "enforce": "",
        "conformance_blocking": "",
        "renovate_automerge": "",
        **dict.fromkeys(PRESENCE_FLAGS.values(), "false"),
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        consumed = False
        if arg in PRESENCE_FLAGS:
            result[PRESENCE_FLAGS[arg]] = "true"
            i += 1
            continue
        retired = _match_retired_flag(arg)
        if retired:
            # Warn and carry on rather than exit 2. The flag is a no-op now, so
            # ignoring it produces exactly the right result, and bootstrap runs
            # inside the automated remediate loop — failing there would block a
            # repo's remediation on a stale argument that changes nothing.
            print(
                f"warning: option {retired!r} was removed: {RETIRED_FLAGS[retired]}."
                " Ignoring it; drop it from the invocation.",
                file=sys.stderr,
            )
            # Skip its value too, in the `--flag value` form.
            if arg == retired and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
            i += 1
            continue
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

    for dest in TRISTATE_FLAGS:
        if result[dest] not in ("", "true", "false"):
            flag = _DEST_TO_FLAG[dest]
            print(
                f"error: {flag} must be 'true' or 'false', got {result[dest]!r}",
                file=sys.stderr,
            )
            sys.exit(2)

    result["system_deps"] = normalize_system_deps(result["system_deps"])
    validate_unit_coverage_fail_under(result["unit_coverage_fail_under"])

    return result


def validate_unit_coverage_fail_under(value: str) -> None:
    """Exit 2 unless *value* is empty or a percent at/above the SDK floor.

    Rejecting a below-floor value here rather than rendering it is deliberate:
    the C002 checker treats such a line as drift (see
    ``extract_tests_yaml_params``), so bootstrap writing one would scaffold a
    file that its own drift check immediately flags — and the only remediation
    (``--resync``) would delete the line the caller just asked for. An app may
    raise its floor above the SDK's; it may not use this flag to duck under it.
    """
    if not value:
        return
    if not value.isdigit():
        print(
            "error: --unit-coverage-fail-under must be a whole coverage percent"
            f" (e.g. 40), got {value!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    if int(value) > 100:
        print(
            "error: --unit-coverage-fail-under is a coverage percent, so it"
            f" cannot exceed 100, got {value!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    if int(value) < extract_mod.SDK_UNIT_COVERAGE_FLOOR:
        print(
            f"error: --unit-coverage-fail-under {value} is below the SDK's own"
            f" floor of {extract_mod.SDK_UNIT_COVERAGE_FLOOR} — apps may raise their unit"
            " coverage floor, not lower it. Omit the flag to inherit the SDK"
            " floor.",
            file=sys.stderr,
        )
        sys.exit(2)


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
pass --enforce true|false (or --renovate-automerge true|false) to also update
renovate.json's enforcement mode, and --resync to pull the resyncable scaffolds'
structure forward without disturbing their per-repo values — which also installs or
updates the local connector review kit (see --connector-review-kit).

The tests gate is not one of these levers. `tests / Tests Gate` becoming a required,
unbypassable status check on the default branch is a GitHub branch-protection setting
that nothing here writes, and it has no prerequisite beyond the check running something
real: it does NOT wait on the four-tier bar or the 85% coverage target, and no flag
below turns it on or off. Make it required as soon as tests.yaml is wired (the
scaffolded tests.yaml names the exact context string in its header). What the flags
below govern is 0-touch — conformance blocking CI, and Renovate merging without a
human — which is what the four-tier bar is a prerequisite for.

options:
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
  --unit-coverage-fail-under N
                              this app's own unit-test coverage floor, rendered into
                              tests.yaml as tests-reusable.yaml's
                              unit-coverage-fail-under input. Omit to inherit the SDK's
                              floor (no line). Must be at or above that floor — apps may
                              raise their bar, not duck under it — and a value that is
                              stays out of C002 entirely, on the scaffold and across
                              --resync alike. (Whether the resulting floor is high enough
                              to ever fail a run is T014's question, not C002's.)
  --use-ghcr-base true|false  resolve the SDK base image from GHCR instead of Harbor,
                              rendered into build-and-publish.yaml as the reusable
                              workflow's use_ghcr_base input. Apps self-select this while
                              the SDK-side default is still false; C002 does not read an
                              opt-in as drift. Omit to auto-detect from an existing
                              build-and-publish.yaml, so a bare re-run preserves the
                              opt-in instead of reverting it to Harbor — that file is
                              always-overwrite. Pass false explicitly to REMOVE the
                              opt-in.
  --enforce true|false        0-touch shorthand: sets BOTH granular levers below at
                              once. Omit to auto-detect from an existing
                              conformance.yaml (else hard-gate). Pass explicitly (either
                              value) to also force-update renovate.json.
                              true  — hard gate: conformance blocks on violations,
                                      Renovate auto-merges when CI is green.
                              false — soft/observe: conformance tracks without blocking,
                                      Renovate raises PRs but humans must merge.
  --conformance-blocking true|false
                              whether conformance findings block CI (conformance.yaml's
                              exit-zero). Overrides --enforce for this lever alone;
                              omit to take the --enforce value (explicit or detected).
  --renovate-automerge true|false
                              whether Renovate merges its own PRs on green
                              (renovate.json). Overrides --enforce for this lever
                              alone; omit to take the --enforce value (explicit or
                              detected). Passing it explicitly force-updates
                              renovate.json, exactly as --enforce does.
  --resync                    re-render the resyncable write-if-absent scaffolds from
                              their canonical templates, so structural catch-up lands
                              in a repo scaffolded by an older bootstrap. Covers
                              tests.yaml and renovate.json, and implies
                              --connector-review-kit. Off by default: these are
                              write-if-absent precisely so apps can customise them, and
                              a bare re-run must never clobber that.
                              Each re-render reuses the per-repo values read back off
                              the existing file — NOT the flags or autodetection — so
                              it cannot silently flip a repo that turned e2e off, that
                              deliberately left its services-script line commented out,
                              that raised its own unit-coverage floor, or that runs
                              Renovate in soft mode. To CHANGE a value,
                              use its own flag (--enforce/--renovate-automerge) or edit
                              the file; those win over --resync for the file they own.
                              Anything hand-edited outside the recognised values is
                              replaced, with the previous content kept alongside as
                              <name>.bak (gitignored) so the edit can be reapplied.
                              A file whose identity can't be read back — a tests.yaml
                              with no parseable app-name, a renovate.json that isn't
                              valid JSON — is skipped with a message rather than
                              rewritten from guessed defaults.
                              No-op on a file that is already canonical, judged by the
                              same comparison C002 uses, so this rewrites exactly what
                              C002 flags as drift and never churns a file it calls
                              clean.
                              Deliberately NOT covered: .gitignore (C003 remediates it
                              additively per missing entry; a re-render would delete an
                              app's own ignores) and contract_schema.lock.json
                              (append-only, owned by `gen-contract-ledger`, with B005/
                              B006 tracking its drift).
                              The connector review kit rides along under different
                              rules — it is centrally owned rather than read back off
                              disk, so it re-renders whole, and its two merge targets
                              (a marked block in CLAUDE.md, one hook entry in
                              .claude/settings.json) preserve everything around them.
                              A target that can't be merged safely skips the kit with a
                              message and leaves the rest of the resync to finish,
                              rather than failing the run.
  --connector-review-kit      install or update the centrally-managed local connector
                              review kit (skill, non-blocking reminder, and rule-fetch
                              script). Implied by --resync; pass it alone to adopt the
                              kit without resyncing tests.yaml/renovate.json. Otherwise
                              opt-in, so a bare bootstrap run never changes local
                              Claude configuration. Existing unmarked review blocks
                              stop for manual migration rather than being overwritten:
                              passed by name that is an error (exit 2, nothing
                              written); implied by --resync it is a skip, so one
                              hand-edited CLAUDE.md cannot block a fleet resync.
  --json                       after the normal output, print one final JSON line:
                              {"skipped": bool, "touched": [...], "unchanged": [...]}.
                              `touched` lists every path this invocation actually wrote
                              (scaffolded/installed/updated/backed-up); use it as the
                              structured, non-prose source for touched_files instead of
                              pattern-matching the prefixed stdout lines above.
  -h, --help                  show this help message and exit
"""
