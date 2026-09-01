# Phase 0 step 8 — Branch freshness and conflict resolution

8. **Branch freshness + conflict resolution** (before reviewing):
   ```bash
   MERGE_STATUS=$(jq -r '.mergeStateStatus' /tmp/PR.json)
   ```

   If `BEHIND` — **sandbox only.** `update-branch` writes to the PR branch and
   needs `contents: write`; the `@sdk-loop` review phase holds a token without
   it, so this 403s. On that lane, review the branch as it is and note it in
   the summary — a base merge cannot introduce a finding in the PR's own
   hunks, which is what the review is about. Do not retry, and do not report
   the 403 as a defect.

   ```bash
   # Tier 1: GitHub-side update (merges base into the PR branch)
   gh api "repos/$REPO/pulls/$PR_NUMBER/update-branch" \
     -X PUT -f update_method=merge 2>/dev/null
   sleep 10
   # Re-fetch — SHA changed after merge
   git fetch origin "$HEAD_REF" && git reset --hard "origin/$HEAD_REF"
   # Re-fetch authoritative PR metadata and diff after the update
   gh pr view "$PR_NUMBER" --repo "$REPO" --json number,state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid,title,body,labels > /tmp/PR.json
   gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
   ```

   If `CONFLICTING`: do NOT attempt a local merge or push — the review is
   read-only (see Runtime). A conflict-resolution merge is the author's
   decision, not the reviewer's. Submit minimal review: "PR has merge conflicts. Please
   rebase or comment `@sdk-review` after resolving conflicts." Set the
   verdict in §3e to `NEEDS_REBASE` (the structured marker is
   `<!-- VERDICT: NEEDS_REBASE -->`); the GHA layer applies the
   `sdk-review-needs-rebase` label from there. EXIT.
