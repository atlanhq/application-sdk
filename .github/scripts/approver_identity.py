"""Which GitHub user posts application-sdk's own code-owner approvals.

application-sdk's `main` ruleset requires a code-owner review, and a GitHub App
cannot be a CODEOWNER, so the automated APPROVE on this repo has to come from a
user account listed in `.github/CODEOWNERS`. That account has always been
`atlan-ci`, whose PAT (`ORG_PAT_GITHUB`) is shared with every other workflow in
the org and so shares one 5,000 req/hr REST quota with all of them.

The workflows now pass the approver explicitly:

    APPROVER_TOKEN  `secrets.SDK_APPROVER_TOKEN || secrets.ORG_PAT_GITHUB`
    APPROVER_LOGIN  `vars.SDK_APPROVER_LOGIN || 'atlan-ci'`

Both default to today's identity, so nothing changes until the new credential is
provisioned. REST quota is per user account, not per token: a second PAT on
`atlan-ci` does not buy a separate quota, so `SDK_APPROVER_LOGIN` exists for the
case where the token belongs to a dedicated machine user.

`atlan-ci` stays recognised as one of ours after the switch. Approvals it posted
before the cutover are still live on open PRs, and the duplicate-approval guards
and the dismiss/withdraw paths have to see them, or a switch would double-approve
some PRs and leave stale approvals standing on others.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

LEGACY_APPROVER_LOGIN = "atlan-ci"


def approver_login(env: Mapping[str, str] = os.environ) -> str:
    """The login the approval token authenticates as (defaults to `atlan-ci`)."""
    return (env.get("APPROVER_LOGIN") or "").strip() or LEGACY_APPROVER_LOGIN


def approver_logins(env: Mapping[str, str] = os.environ) -> frozenset[str]:
    """Every login whose signed approvals count as ours: current plus legacy."""
    return frozenset({approver_login(env), LEGACY_APPROVER_LOGIN})
