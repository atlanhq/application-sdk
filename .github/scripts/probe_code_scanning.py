"""Resolve whether GitHub code scanning is available for this repository.

Code scanning is a GitHub Advanced Security feature: free on public
repos, licensed on private ones. ``github/codeql-action/upload-sarif``
fails the whole job wherever it is unavailable ("Advanced Security must
be enabled for this repository to use code scanning") — which is what
FND-1149 was: 77 of the 82 repos carrying
``conformance-upload-sarif.yaml`` are private, so every matrix leg
failed on every push to main.

Reads ``GITHUB_REPOSITORY`` (and ``GH_TOKEN``, consumed by ``gh``), then
appends ``available=true`` or ``available=false`` to the file named by
``GITHUB_OUTPUT``. The upload step ANDs that into its ``if:``.

Two properties held on purpose:

- **Always exits 0.** Code Scanning marks a tool as "reporting errors"
  whenever the workflow uploading its SARIF fails, so this workflow must
  never fail — including when the probe itself cannot reach the API.
- **Fails closed, but never silently.** An unreachable or malformed API
  response yields ``available=false`` (today's behaviour minus the
  failing job) *and* a ``::notice::`` naming the transport failure, so a
  suppressed upload on an eligible repo stays distinguishable in the log
  from genuine ineligibility.

Gating on eligibility rather than on visibility alone means a private
repo starts uploading the moment GHAS is licensed, with no edit here.
Querying ``code-scanning/analyses`` instead was rejected: it 404s on a
repo with no analyses yet, which is exactly a newly-licensed repo, so
that check would lock itself off permanently.

``bootstrap`` vendors this file into every consumer repo and overwrites
it on every run, so a consumer cannot fix it locally — the next
bootstrap reverts the fix. It therefore has to be lint-clean under the
strictest ruff config in the fleet, not just this repo's (FND-445):
docstrings everywhere, every line inside 88 columns, and every exploded
call closed by a magic trailing comma so ``ruff format`` is a no-op at
any configured line length.
``tests/test_bootstrap_scaffold_lint.py`` in the conformance package
enforces both; ``.github/scripts/tests/test_probe_code_scanning.py``
covers the behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# `security_and_analysis` is admin-only on the repos endpoint, so a
# non-admin GITHUB_TOKEN may read `visibility` yet see the GHAS block
# absent even on a licensed private repo. That is reported as its own
# value rather than folded into "disabled", so the log distinguishes
# "not licensed" from "this token cannot see the licence".
ABSENT = "absent"

# Neither field could be read at all — the API call itself failed.
UNKNOWN = "unknown"


def decide(visibility: str, advanced_security: str) -> bool:
    """Return whether code scanning is usable for this repository.

    Public repos get code scanning inherently and never carry an
    ``advanced_security`` key, hence the two-branch test.
    """
    return visibility == "public" or advanced_security == "enabled"


def probe(repo: str) -> tuple[str, str, str]:
    """Return ``(visibility, advanced_security, error)`` for *repo*.

    ``error`` is empty on success. Otherwise it says why the GitHub API
    could not be read and both other values are ``UNKNOWN``, which
    ``decide`` resolves to unavailable.
    """
    endpoint = f"repos/{repo}"
    try:
        proc = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return UNKNOWN, UNKNOWN, f"could not run `gh api {endpoint}`: {exc}"

    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        detail = lines[0] if lines else f"exit status {proc.returncode}"
        return UNKNOWN, UNKNOWN, f"`gh api {endpoint}` failed: {detail}"

    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        return UNKNOWN, UNKNOWN, f"`gh api {endpoint}` returned non-JSON: {exc}"

    if not isinstance(payload, dict):
        return UNKNOWN, UNKNOWN, f"`gh api {endpoint}` returned no object"

    visibility = payload.get("visibility") or UNKNOWN
    analysis = payload.get("security_and_analysis")
    advanced = analysis.get("advanced_security") if isinstance(analysis, dict) else None
    status = advanced.get("status") if isinstance(advanced, dict) else None
    return str(visibility), str(status or ABSENT), ""


def emit(message: str) -> None:
    """Write one line to stdout.

    GitHub Actions reads workflow commands (``::notice::``) from a
    step's stdout, so printing *is* this script's output mechanism —
    hence the ``T201`` suppression here rather than a ruff ignore in
    every repo this file is vendored into.
    """
    print(message)  # noqa: T201


def record(available: bool) -> None:
    """Append ``available=`` to the file named by ``GITHUB_OUTPUT``.

    Outside Actions there is no such file, so the assignment goes to
    stdout instead of raising.
    """
    line = f"available={'true' if available else 'false'}"
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        emit(line)
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def main() -> int:
    """Resolve availability, record it, and always return 0."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        visibility, advanced_security, error = probe(repo)
    else:
        visibility, advanced_security = UNKNOWN, UNKNOWN
        error = "GITHUB_REPOSITORY is unset"

    available = decide(visibility, advanced_security)
    if error:
        emit(
            f"::notice::Code scanning availability could not be resolved "
            f"({error}). Skipping SARIF upload. This is a probe failure, "
            f"not a verdict that the repository is ineligible — a public "
            f"or GHAS-licensed repo reaching this line is a transport "
            f"problem, not a licence one.",
        )
    elif not available:
        emit(
            f"::notice::Code scanning unavailable (visibility="
            f"{visibility}, advanced_security={advanced_security}). "
            f"Skipping SARIF upload. The conformance gate result and the "
            f"connector-pulse dashboard are unaffected.",
        )
    record(available)
    return 0


if __name__ == "__main__":
    sys.exit(main())
