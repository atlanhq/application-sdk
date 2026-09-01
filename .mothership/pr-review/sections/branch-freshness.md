# Phase 0 step 8 (BEHIND branch) — mothership sandbox only

The `CONFLICTING` half of step 8 stays in ORCHESTRATION.md: it applies to both
lanes, and `@sdk-loop` depends on the `NEEDS_REBASE` verdict it produces. Only
the `BEHIND` update lives here, because only the sandbox lane can perform it.

8. **Branch is `BEHIND` — update it, then re-read what changed.**

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

   NOTE — unresolved tension, do not silently "fix" it either way. `update-branch`
   is a WRITE to the PR branch, and the Runtime section of ORCHESTRATION.md
   states flatly that "the review never writes to the branch on either lane".
   Both statements are currently in the playbook. On `@sdk-loop` the question is
   moot (no write scope, and FND-1185 moved branch duty to the prep phase, which
   has one). On the sandbox lane it is a live contradiction and wants a decision
   from a human, not a reviewer picking one sentence over the other mid-run.
