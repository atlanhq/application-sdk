"""The code-owner approval, as the workflow's last step.

Modelled on sdk-review's stamp (`.github/scripts/sdk_review_approve.py`):

- Two tokens, deliberately. `atlan-ci` is a CODEOWNER, so the APPROVE review
  must carry that identity; GitHub Apps cannot be code owners. Nothing else
  needs it. The fleet App token does every read and the dismissals, and the
  `atlan-ci` PAT is spent on exactly one request, the APPROVE, because it shares
  one hourly quota with every other `atlan-ci` workflow.
- A signature marks lens's approvals, so lens only ever finds and withdraws
  its own, never a person's or sdk-review's.
- Idempotent: an approval already on this head with the signature is not
  posted again.

The review step reads untrusted PR text and talks to a model, so it never holds
the approver token. It only writes a decision file. This step runs no model and
reads no PR text; it re-checks the PR and acts.

Approve only when ALL hold:
  1. every finding at every level is fixed or dismissed, and the review covered
     the whole change (nothing incomplete, no files left pending);
  2. the PR is open and not a draft;
  3. its head is still the head lens reviewed (the ruleset also dismisses
     approvals on push);
  4. the approver is not the PR's author (GitHub refuses self-approval);
  5. there is no lens approval on this head already.

When a review is not ready, lens withdraws its earlier approvals (e.g. after
`/lens force` on the same head finds a new problem).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .github import GitHub, GitHubError

SIGNATURE = "**lens: ready to merge**"
APPROVER_LOGIN = "atlan-ci"  # the CODEOWNERS user whose PAT is APPROVER_TOKEN


def decision_for(res: Any) -> dict[str, Any]:
    """What the approval step should do, from this run's result (no side effects)."""
    st = res.state
    # A dismissal can make a reviewed head ready; it is judged the same way.
    if (
        res.action not in ("reviewed", "dismissed")
        or st is None
        or not st.reviewed_head
    ):
        return {"action": "none"}
    ready = (
        not res.failed
        and not res.incomplete
        and not st.pending_files
        and not st.open_findings()  # every level, nits included
    )
    if ready:
        return {"action": "approve", "head": st.reviewed_head, "round": st.round}
    return {"action": "withdraw", "head": st.reviewed_head}


def write_decision(path: str | None, pr: int, decision: dict[str, Any]) -> None:
    if path:
        Path(path).write_text(json.dumps({"pr": pr, **decision}), encoding="utf-8")


def _lens_approvals(gh: GitHub, number: int, login: str) -> list[dict[str, Any]]:
    return [
        r
        for r in gh.reviews(number)
        if (r.get("user") or {}).get("login") == login
        and r.get("state") == "APPROVED"
        and (r.get("body") or "").startswith(SIGNATURE)
    ]


def apply(
    gh: GitHub, approver: GitHub, decision: dict[str, Any], login: str = APPROVER_LOGIN
) -> str:
    """Carry out a decision. `gh` (App token) reads and dismisses; `approver`
    (the code owner's token) is used for the APPROVE call only."""
    action, number = decision.get("action"), int(decision.get("pr") or 0)
    if action not in ("approve", "withdraw") or not number:
        return "nothing to do"
    mine = _lens_approvals(gh, number, login)
    if action == "withdraw":
        for r in mine:
            gh.dismiss_review(
                number, int(r["id"]), "lens: the latest review is not ready to merge."
            )
        return (
            f"withdrew {len(mine)} lens approval(s)"
            if mine
            else "no lens approval to withdraw"
        )
    pr = gh.pr(number)
    head = (pr.get("head") or {}).get("sha")
    if pr.get("state") != "open" or pr.get("draft"):
        return "not approving: the PR is closed or a draft"
    if head != decision.get("head"):
        return "not approving: the PR head moved since lens reviewed it"
    if (pr.get("user") or {}).get("login") == login:
        return "not approving: the approver authored this PR"
    if any(r.get("commit_id") == head for r in mine):
        return "already approved this head"
    approver.approve(
        number,
        head,
        f"{SIGNATURE} — every finding at every level is resolved "
        f"(round {decision.get('round', '?')}).",
    )
    return f"approved {head[:9]} as {login}"


def run_step(repo: str, decision_path: str) -> int:
    p = Path(decision_path)
    if not p.exists():
        print("lens approve: no decision — the review did not reach a verdict")
        return 0
    token = os.environ.get("APPROVER_TOKEN", "")
    if not token:
        print("::warning::lens approve: no APPROVER_TOKEN; not approving")
        return 0
    decision = json.loads(p.read_text(encoding="utf-8"))
    try:
        print(
            f"lens approve: {apply(GitHub(repo), GitHub(repo, token=token), decision)}"
        )
    except GitHubError as e:
        # The review already posted its verdict and status; an approval that
        # cannot be posted is surfaced, and must not turn a finished review red.
        print(f"::warning::lens approve: could not act on the decision: {e}")
    return 0
