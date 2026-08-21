#!/usr/bin/env python3
"""Preflight for trivy-container.yaml: report missing secrets as a job failure.

Why this exists rather than `required: true` on the `secrets:` declaration:

A reusable workflow that declares `secrets: {FOO: {required: true}}` and is
called with an *explicit* `secrets:` block omitting FOO does not fail the job —
it fails to parse. The run ends as `startup_failure` with **zero jobs**, so it
emits no check run at all: absent from `gh pr checks`, not red, not pending.
Nothing surfaces it, and no required check can ever depend on it. Fleet-wide
that produced 32 connector repos whose `trivy.yml` had been dead since
2026-08-12 with no signal (FND-447).

`required: true` also buys almost nothing in exchange: it only asserts the
caller *mentions* the name, never that the secret exists or is non-empty, and
every `secrets: inherit` caller satisfies it unconditionally. So the secrets are
declared optional and validated here instead, inside the job, where a missing
one is a normal red step with an actionable message.

Run standalone (`python3 check_trivy_secret_contract.py`) reading the
environment, or via the tested functions in this module
(`.github/scripts/tests/test_check_trivy_secret_contract.py`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# Secrets the scan cannot run without under any configuration: the composite
# does a `docker login cgr.dev` with them before it can build the image.
ALWAYS_REQUIRED = ("CHAINGUARD_USERNAME", "CHAINGUARD_PASSWORD")

# Only needed as the fallback for the cross-repo allowlist checkout and the
# BuildKit private-dependency secret. When the fleet App token minted, nothing
# reads it, so demanding it would fail callers that are correctly configured.
REQUIRED_WITHOUT_APP_TOKEN = "ORG_PAT_GITHUB"

REMEDY = (
    "Fix the caller's trivy.yml: replace its explicit `secrets:` block with "
    "`secrets: inherit`, as the reusable's usage docstring recommends. An "
    "explicit block passes only the secrets it names, and every name it omits "
    "arrives here empty."
)


def missing_secrets(env: Mapping[str, str]) -> list[str]:
    """Names of secrets the scan needs that arrived empty or unset.

    `env` is the step's environment, where each secret has been mapped to a
    same-named variable. GitHub renders an unpassed secret as the empty string
    rather than omitting the variable, so absent and empty are one case.
    """
    missing = [name for name in ALWAYS_REQUIRED if not env.get(name)]
    app_token_minted = env.get("APP_TOKEN_MINTED", "").strip() != ""
    if not app_token_minted and not env.get(REQUIRED_WITHOUT_APP_TOKEN):
        missing.append(REQUIRED_WITHOUT_APP_TOKEN)
    return missing


def failure_message(missing: list[str]) -> str:
    """Operator-facing explanation of which secrets are missing and what to do."""
    return (
        "trivy-container.yaml did not receive these secrets: "
        f"{', '.join(missing)}.\n\n{REMEDY}"
    )


def main() -> int:
    missing = missing_secrets(os.environ)
    if missing:
        print(failure_message(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
