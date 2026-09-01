# §3c mechanics — what reacts to the verdict marker

Diagnostic reference. The rule the reviewer needs — never approve or
label from inside the orchestration; emit the marker and stop — lives in
ORCHESTRATION.md §3c. This file exists for the rarer question of why a
posted verdict did not produce the expected label or approval.

- **Approval**: `sdk-review-approve-on-verdict.yml` fires on
  `issue_comment: created` from `mothership-ai[bot]` with the
  `<!-- SDK_REVIEW -->` marker (within ~5s of the verdict comment
  landing). It parses the verdict from the structured
  `<!-- VERDICT: X -->` marker in §3e, applies the
  `sdk-review-approved` / `sdk-review-needs-human` /
  `sdk-review-needs-rebase` labels, sets the `sdk-review` commit
  status, and posts the formal `atlan-ci` approval if the verdict is
  `READY_TO_MERGE`. `sdk-review.yml`'s "Approve PR as atlan-ci" step
  runs the same logic after the SSE stream ends as a fallback —
  idempotency guards (label present + no existing approval) prevent
  double-approval. atlan-ci is in CODEOWNERS, so its approval
  satisfies `require_code_owner_review` on `main`;
  `mothership-ai[bot]` is a GitHub App and can't be.
- **Dismiss on human activity**: `sdk-review-dismiss-on-human.yml`
  fires on `issue_comment` / `pull_request_review` from humans and
  dismisses the atlan-ci approval + strips the label. So the bot can
  unblock merges by itself until a human pushes back.
- **Reset on push**: `sdk-review-reset-on-push.yml` fires on
  `pull_request: synchronize` and strips the label + flips the
  `sdk-review` status to pending on the new HEAD. Branch protection
  separately auto-dismisses the approval (`dismiss_stale_reviews_on_push`).
- **CI-failure downgrade**: `sdk-review-downgrade-on-ci-failure.yml`
  fires on `check_suite: completed`; if a non-sdk-review check
  failed on a HEAD that carries `sdk-review-approved`, it strips
  the label, dismisses the approval, and flips status to failure.

