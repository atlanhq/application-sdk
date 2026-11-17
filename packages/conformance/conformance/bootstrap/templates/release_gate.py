"""Decide whether a release PR has passed its release gate.

A release PR (a ``bump-version*`` branch, labelled ``release`` by the SDK's
release-version-bump workflow) may not merge until its full release gate has
run AND passed on its head commit. Every other PR passes. The caller fetches
the head commit's ``gate-check`` status in a separate, ``continue-on-error``
step and passes the state in a file::

    python .github/scripts/release_gate.py \
      --head-ref "$HEAD_REF" --labels "$LABELS" \
      --gate-check-state-file gate-check-state.txt

Every argument is passed unconditionally so the caller's ``run:`` block
stays straight-line (per docs/standards/ci.md): no ``if``, no ``||``, no
parameter-expansion tricks. All the branching is here, where a test can
reach it, and none of it touches the network.

The ``gate-check`` status is the release gate's verdict (FND-3839). A
``/gate-check`` PR comment posts it as ``pending`` and dispatches the gated
workflows; once the last of them finishes it becomes ``success`` only if all
of them passed, and this gate is re-run to read it. So:

* ``success`` passes;
* ``pending`` fails - the gate is still running;
* ``failure`` / ``error`` fail - something in the gate failed;
* no status fails - nobody has asked yet.

Labels no longer pass this gate. They used to (``gate-check`` / ``e2e``), but
a label is not a verdict: it said a run was requested, and the run itself was
started by the label's ``labeled`` event, which also started duplicate runs on
the same commit. The status is bound to the commit, so a push moves the PR onto
a commit with no status and the gate re-blocks until ``/gate-check`` is asked
again on the new head.

Only a status ``github-actions[bot]`` created counts (FND-3947). That is the
identity ``test-commands.yaml`` posts the verdict with (``github.token``).
Anyone with write access can post a ``gate-check`` status through the API, and
on a bump PR this gate is the only blocker: the gated reusables skip their jobs
there, and a skipped required check reads as a pass. So a status from anyone
else is ignored, never read as a verdict.

An empty or unreadable state file means the lookup failed, and the gate
FAILS on it. That is the opposite of the connector-review gate, and
deliberate: a release that skips its gate is worse than a re-run of a
two-minute check.

A release PR is also refused, whatever its ``gate-check`` state, while the
app is pinned to an UNRELEASED Application SDK. That is any of:

* ``uv.lock`` resolves an ``atlan-application-sdk*`` package from anywhere
  but a package index (a git ref, a local path, a URL);
* the version it locks is a pre-release (``rc``, ``a``, ``b``, ``.dev``);
* ``pyproject.toml`` routes one through ``[tool.uv.sources]`` to anything
  but a named index.

Pinning an SDK branch is how an SDK change is tried on one app before the
SDK releases it. It is fine on a feature branch and wrong in a release:
the image would ship SDK code that no SDK release contains and no other
app has run. The check reads the two files the caller checked out, never
the network, and runs before the status is read so the author learns of
the pin without paying for a ``/gate-check``. A ``uv.lock`` that cannot be
parsed fails the gate; a repo with no ``uv.lock`` has nothing to check.

``bootstrap`` vendors this file into every consumer repo and overwrites
it on every run, so a consumer cannot fix it locally - the next
bootstrap reverts the fix. It therefore has to be lint-clean under the
strictest ruff config in the fleet, not just this repo's (FND-445).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tomllib

RELEASE_LABEL = "release"
BUMP_BRANCH_PREFIX = "bump-version"
VERDICT_CREATOR = "github-actions[bot]"

# Every distribution the SDK repo publishes starts with this.
SDK_PREFIX = "atlan-application-sdk"
# A final release, optionally a post-release: 3.57.0, 3.57.0.post1.
RELEASED_VERSION = re.compile(r"\d+(\.\d+)*(\.post\d+)?")
PIN_HINT = "Pin a released SDK version, re-lock, then /gate-check."
LOCK_UNREADABLE = "uv.lock could not be parsed, so the SDK pin is unknown."

# Kept at module level so each line fits inside the tightest wrap in the
# fleet and `ruff format` is a no-op at every width (see FND-445).
MISSING_HINT = "Release PR CI is opt-in. Comment /gate-check to run it."
PENDING_HINT = "The release gate is still running on this commit."
FAILED_HINT = "The release gate failed on this commit. Fix it, then /gate-check."
PASSED = "The release gate passed on this commit - gate passes."


def parse_labels(raw: str) -> list[str]:
    """Return label names from ``toJson(...labels.*.name)``, else ``[]``."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [label for label in payload if isinstance(label, str)]


def read_state(path: str) -> str:
    """Return the newest ``gate-check`` state ``VERDICT_CREATOR`` posted, else ``""``.

    ``gh api .../statuses --jq`` prints one ``<state> <creator login>`` line
    per status, newest first. The first line whose creator is
    ``VERDICT_CREATOR`` is the verdict. A line from anyone else, or in any
    other shape, is skipped.
    """
    file = pathlib.Path(path)
    if not path or not file.is_file():
        return ""
    for line in file.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == VERDICT_CREATOR:
            return fields[0]
    return ""


def is_release_pr(head_ref: str, labels: list[str]) -> bool:
    """Return whether this is a release PR: a bump branch or ``release`` label.

    The branch is the reliable signal: the label is added after the PR is
    opened, so the ``opened`` event's payload can lack it.
    """
    return head_ref.startswith(BUMP_BRANCH_PREFIX) or RELEASE_LABEL in labels


def _read_toml(path: str) -> dict | None:
    """Return the parsed TOML file, ``{}`` if absent, ``None`` if unreadable."""
    file = pathlib.Path(path)
    if not path or not file.is_file():
        return {}
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _is_sdk(name: object) -> bool:
    """Return whether ``name`` is a distribution the SDK repo publishes."""
    return isinstance(name, str) and name.lower().startswith(SDK_PREFIX)


def _locked_pins(lock: dict) -> list[str]:
    """Return one finding per SDK package ``uv.lock`` pins unreleased."""
    found = []
    packages = lock.get("package", [])
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict) or not _is_sdk(package.get("name")):
            continue
        name = package["name"]
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            kind = "/".join(sorted(source)) if isinstance(source, dict) else "?"
            found.append(f"{name} is locked from a {kind} source")
            continue
        version = str(package.get("version", ""))
        if not RELEASED_VERSION.fullmatch(version):
            found.append(f"{name} is locked to pre-release {version}")
    return found


def _source_pins(pyproject: dict) -> list[str]:
    """Return one finding per SDK package ``[tool.uv.sources]`` redirects."""
    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    found = []
    for name, source in sources.items() if isinstance(sources, dict) else []:
        if not _is_sdk(name):
            continue
        if isinstance(source, dict) and set(source) <= {"index", "marker"}:
            continue
        found.append(f"{name} is redirected by [tool.uv.sources]")
    return found


def unreleased_sdk_pins(lock_path: str, pyproject_path: str) -> list[str]:
    """Return why this checkout is pinned to an unreleased SDK, else ``[]``.

    The SDK repo itself (a project named ``atlan-application-sdk*``) locks
    its own packages from the workspace, so it is never a finding.
    """
    pyproject = _read_toml(pyproject_path) or {}
    project = pyproject.get("project")
    if isinstance(project, dict) and _is_sdk(project.get("name")):
        return []
    lock = _read_toml(lock_path)
    if lock is None:
        return [LOCK_UNREADABLE]
    return _locked_pins(lock) + _source_pins(pyproject)


def decide(
    head_ref: str,
    labels: list[str],
    state: str,
    pins: list[str] | None = None,
) -> tuple[bool, str]:
    """Return (passes, message) for the head commit's ``gate-check`` state.

    ``state`` is ``""`` when there is no status or it could not be read.
    ``pins`` are the findings of ``unreleased_sdk_pins``; any of them fails
    a release PR before the state is looked at.
    """
    if not is_release_pr(head_ref, labels):
        return True, "Not a release PR - gate passes."
    if pins:
        return False, f"Unreleased SDK pin: {'; '.join(pins)}. {PIN_HINT}"
    if state == "success":
        return True, PASSED
    if state == "pending":
        return False, PENDING_HINT
    if state in {"failure", "error"}:
        return False, FAILED_HINT
    return False, MISSING_HINT


def _build_parser() -> argparse.ArgumentParser:
    """Build the gate's argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-ref", default="", help="the PR's head branch")
    parser.add_argument(
        "--labels",
        default="[]",
        help="the PR's label names as a JSON array",
    )
    parser.add_argument(
        "--gate-check-state-file",
        default="",
        help="file holding the head commit's `gate-check` status state",
    )
    parser.add_argument("--lock-file", default="", help="the app's uv.lock")
    parser.add_argument("--pyproject", default="", help="the app's pyproject")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the gate and return the process exit code."""
    args = _build_parser().parse_args(argv)
    passes, message = decide(
        args.head_ref,
        parse_labels(args.labels),
        read_state(args.gate_check_state_file),
        unreleased_sdk_pins(args.lock_file, args.pyproject),
    )
    if passes:
        print(message)  # noqa: T201
        return 0
    print(f"::error::{message}")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
