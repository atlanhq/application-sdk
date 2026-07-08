#!/usr/bin/env python3
"""PR title convention driver.

Decides whether a PR title matches the conventional-commit type its changed
files require, per the precedence rules documented in
.github/workflows/pr-title-convention.yaml:

  0. Version-bump / release automation -> ignored entirely.
  1. application_sdk/ touched         -> no restriction.
  2. Dockerfile / entrypoint.sh       -> feat:/fix: required.
  3. contract-toolkit/ core           -> feat(contract-toolkit):/fix(contract-toolkit): required.
  4. packages/conformance/ core       -> feat(conformance):/fix(conformance): required.
  5. everything else                  -> chore:/ci: required.

Usage:
    python3 pr_title_convention.py \
        --pr-title "<title>" \
        --head-ref "<branch>" \
        --changed-files <path to newline-delimited changed-file list> \
        --comment-out <path to write the sticky-comment markdown on violation>

Writes `violation`, `expected`, and `error_message` to $GITHUB_OUTPUT (or
prints `key=value` lines if $GITHUB_OUTPUT is unset). Always exits 0 — the
calling workflow gates on the `violation` output in a separate step.
"""

import argparse
import fnmatch
import os
import re
import sys

RELEASE_RE = re.compile(r"^chore(\([^)]*\))?: release ")
BUMP_RE = re.compile(r"^Bump version to ")

DOCKER_RE = re.compile(r"^(feat|fix)(\([^)]*\))?!?:")
CT_RE = re.compile(r"^(feat|fix)\(contract-toolkit\)!?:")
CF_RE = re.compile(r"^(feat|fix)\(conformance\)!?:")
CHORE_RE = re.compile(r"^(chore|ci)(\([^)]*\))?!?:")

DOCKER_IMAGE_FILES = ("Dockerfile", "entrypoint.sh")

CT_EXEMPT_GLOBS = (
    "contract-toolkit/docs/*",
    "contract-toolkit/examples/*",
    "contract-toolkit/scripts/*",
    "contract-toolkit/tests/*",
    "contract-toolkit/.github/*",
)
CF_EXEMPT_GLOBS = (
    "packages/conformance/tests/*",
    "packages/conformance/uv.lock",
    "packages/conformance/*/package-lock.json",
    "packages/conformance/*/yarn.lock",
)

_VALIDATORS = {
    "docker-img": (DOCKER_RE, "docker-img"),
    "ct-core": (CT_RE, "ct-core"),
    "cf-core": (CF_RE, "cf-core"),
    "other": (CHORE_RE, "chore-ci"),
}

_LOG_MESSAGES = {
    "sdk": "application_sdk/ touched — title type unrestricted. ✅",
    ("docker-img", False): "Docker image file change with a valid feat/fix title. ✅",
    ("docker-img", True): (
        "Violation: Docker image file change (Dockerfile/entrypoint.sh) must "
        "use feat: or fix:."
    ),
    ("ct-core", False): "contract-toolkit core change with a valid scoped title. ✅",
    ("ct-core", True): (
        "Violation: contract-toolkit core change must use "
        "feat(contract-toolkit): or fix(contract-toolkit):."
    ),
    (
        "cf-core",
        False,
    ): "packages/conformance core change with a valid scoped title. ✅",
    ("cf-core", True): (
        "Violation: conformance core change must use feat(conformance): or "
        "fix(conformance):."
    ),
    ("other", False): "Non-source change with a chore/ci title. ✅",
    ("other", True): "Violation: non-source change must use chore: or ci:.",
}

_ERROR_MESSAGES = {
    "docker-img": (
        "Docker image changes (Dockerfile/entrypoint.sh) must use 'feat:' or 'fix:'."
    ),
    "ct-core": (
        "contract-toolkit core changes must use 'feat(contract-toolkit):' or "
        "'fix(contract-toolkit):'."
    ),
    "cf-core": (
        "conformance core changes must use 'feat(conformance):' or "
        "'fix(conformance):'."
    ),
    "chore-ci": (
        "Non-source changes must use 'chore:' or 'ci:' (feat:/fix: are reserved "
        "for application_sdk/, Dockerfile/entrypoint.sh, contract-toolkit core, "
        "and packages/conformance core)."
    ),
}

_COMMENTS = {
    "docker-img": """\
### \U0001f3f7️ Docker image changes need a `feat`/`fix` title

This PR changes a file baked into the published Docker image
(`Dockerfile` and/or `entrypoint.sh`), so it's user-facing runtime
behavior and its title must be:

- `feat: …` / `feat(scope): …`
- `fix: …` / `fix(scope): …`

Editing the PR title re-runs this check and clears this comment automatically.
""",
    "ct-core": """\
### \U0001f3f7️ contract-toolkit changes need a scoped `feat`/`fix` title

This PR changes `contract-toolkit/` source, so its title must use the
`contract-toolkit` scope:

- `feat(contract-toolkit): …`
- `fix(contract-toolkit): …`

(Changes confined to `contract-toolkit/{docs,examples,scripts,tests,.github}/`
should instead be `chore:`/`ci:`.)

Editing the PR title re-runs this check and clears this comment automatically.
""",
    "cf-core": """\
### \U0001f3f7️ conformance package changes need a scoped `feat`/`fix` title

This PR changes `packages/conformance/` source, so its title must use the
`conformance` scope:

- `feat(conformance): …`
- `fix(conformance): …`

(Changes confined to `packages/conformance/tests/` or lock files
such as `uv.lock`/`package-lock.json` should instead be `chore:`/`ci:`.)

Editing the PR title re-runs this check and clears this comment automatically.
""",
    "chore-ci": """\
### \U0001f3f7️ Non-source PR shouldn't use a `feat`/`fix` title

This PR does not change shipped source (`application_sdk/`, the
Docker image inputs `Dockerfile`/`entrypoint.sh`, `contract-toolkit/`
core, or `packages/conformance/` core), so a `feat:`/`fix:` title
would add it to the user-facing SDK changelog as a real feature/fix
— exactly the noise this check prevents.

**Use a non-surfacing conventional type instead:**
- `ci:` / `ci(scope):` — changes to CI configuration and scripts
- `chore:` / `chore(scope):` — everything else not touching shipped source

_Example:_ `ci(publish): use crane to copy large images GHCR → Docker Hub`

Editing the PR title re-runs this check and clears this comment automatically.
""",
}


def is_version_bump_pr(pr_title: str, head_ref: str) -> bool:
    """True for generated version-bump / release PRs, which this guard ignores."""
    return (
        head_ref.startswith("bump-version")
        or bool(RELEASE_RE.match(pr_title))
        or bool(BUMP_RE.match(pr_title))
    )


def classify_files(files: list) -> str:
    """Return the highest-precedence zone touched by ``files``.

    One of "sdk", "docker-img", "ct-core", "cf-core", or "other".
    """
    sdk = docker_img = ct_core = cf_core = False
    for f in files:
        if f.startswith("application_sdk/"):
            sdk = True
        elif f in DOCKER_IMAGE_FILES:
            docker_img = True
        elif any(fnmatch.fnmatch(f, glob) for glob in CT_EXEMPT_GLOBS):
            continue
        elif f.startswith("contract-toolkit/"):
            ct_core = True
        elif any(fnmatch.fnmatch(f, glob) for glob in CF_EXEMPT_GLOBS):
            continue
        elif f.startswith("packages/conformance/"):
            cf_core = True

    if sdk:
        return "sdk"
    if docker_img:
        return "docker-img"
    if ct_core:
        return "ct-core"
    if cf_core:
        return "cf-core"
    return "other"


def validate(zone: str, pr_title: str):
    """Return (violation, expected) for a title against the given zone."""
    if zone == "sdk":
        return False, ""
    regex, expected = _VALIDATORS[zone]
    if regex.match(pr_title):
        return False, ""
    return True, expected


def _read_changed_files(path: str) -> list:
    with open(path) as fh:
        return [line.strip() for line in fh if line.strip()]


def _write_output(key: str, value: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def run(pr_title: str, head_ref: str, changed_files_path: str, comment_out_path: str):
    """Core decision logic, returned as (violation, expected) for testing."""
    if is_version_bump_pr(pr_title, head_ref):
        print("Version-bump / release PR — ignored by this guard.")
        return False, ""

    files = _read_changed_files(changed_files_path)
    if not files:
        print("No changed files reported; nothing to validate.")
        return False, ""

    zone = classify_files(files)
    violation, expected = validate(zone, pr_title)
    log_key = zone if zone == "sdk" else (zone, violation)
    print(_LOG_MESSAGES[log_key])

    if violation:
        with open(comment_out_path, "w") as fh:
            fh.write(_COMMENTS[expected])

    return violation, expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument(
        "--changed-files",
        required=True,
        help="Path to a newline-delimited list of changed file paths",
    )
    parser.add_argument(
        "--comment-out",
        required=True,
        help="Path to write the sticky-comment markdown when the title is invalid",
    )
    args = parser.parse_args()

    print(f"PR title: {args.pr_title}")
    violation, expected = run(
        args.pr_title, args.head_ref, args.changed_files, args.comment_out
    )

    _write_output("violation", "true" if violation else "false")
    _write_output("expected", expected)
    _write_output("error_message", _ERROR_MESSAGES.get(expected, ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
