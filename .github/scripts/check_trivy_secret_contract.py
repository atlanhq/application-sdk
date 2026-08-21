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

# Nothing is needed unconditionally any more. CHAINGUARD_USERNAME /
# CHAINGUARD_PASSWORD used to sit here, and were the whole problem: they exist
# only as repo-level secrets on application-sdk, so no caller could ever supply
# them, and no caller builds from cgr.dev in the first place. Both are gone from
# the reusable's contract and the composite's login is now conditional.
ALWAYS_REQUIRED: tuple[str, ...] = ()

# Only needed as the fallback for the cross-repo allowlist checkout and the
# BuildKit private-dependency secret. When the fleet App token minted, nothing
# reads it, so demanding it would fail callers that are correctly configured.
REQUIRED_WITHOUT_APP_TOKEN = "ORG_PAT_GITHUB"

REMEDY = (
    "Fix the caller: pass `secrets: inherit` instead of an explicit `secrets:` "
    "block, or add the missing name to the block. An explicit block passes only "
    "the secrets it names, and every name it omits arrives here empty. Better "
    "still, migrate to build-and-scan.yaml -- this workflow is deprecated."
)


def secrets_present(env: Mapping[str, str]) -> dict[str, bool]:
    """Reduce the environment to "did this secret arrive non-empty?" per name.

    This is the only function that touches secret *values*, and it returns
    booleans, so no value can reach the reporting path below. Keeping that
    boundary explicit is what makes the failure message provably name-only --
    CodeQL flagged the previous shape, which handed the whole environment to the
    function whose result gets printed, as clear-text logging of a secret.

    GitHub renders an unpassed secret as the empty string rather than omitting
    the variable, so absent and empty are one case. A value that is only
    whitespace is treated as absent too: it would fail downstream anyway, and
    reporting it here beats a cryptic auth error later.
    """
    names = (*ALWAYS_REQUIRED, REQUIRED_WITHOUT_APP_TOKEN, "APP_TOKEN_MINTED")
    return {name: bool(env.get(name, "").strip()) for name in names}


def missing_secrets(present: Mapping[str, bool]) -> list[str]:
    """Names of secrets the scan needs that did not arrive.

    Takes the presence map from `secrets_present`, never the environment: every
    name returned is a module-level constant, so the caller can print the result
    without any possibility of echoing a value.
    """
    missing = [name for name in ALWAYS_REQUIRED if not present.get(name)]
    if not present.get("APP_TOKEN_MINTED") and not present.get(
        REQUIRED_WITHOUT_APP_TOKEN
    ):
        missing.append(REQUIRED_WITHOUT_APP_TOKEN)
    return missing


def failure_message(missing: list[str]) -> str:
    """Operator-facing explanation of which secrets are missing and what to do.

    `missing` holds names only, by construction -- see `missing_secrets`.
    """
    return (
        "trivy-container.yaml did not receive these secrets: "
        f"{', '.join(missing)}.\n\n{REMEDY}"
    )


# Built from module-level constants only, never from the environment. main()
# prints this and nothing else so CodeQL cannot treat the print as a sink for
# secret values. ALWAYS_REQUIRED is empty today, so the only name this can
# report is ORG_PAT_GITHUB -- the sole failure path of missing_secrets().
FAILURE_TEXT = failure_message([*ALWAYS_REQUIRED, REQUIRED_WITHOUT_APP_TOKEN])


def main() -> int:
    if missing_secrets(secrets_present(os.environ)):
        print(FAILURE_TEXT, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
