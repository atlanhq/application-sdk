# SDK Review v2 — Setup Instructions

## 1. Add the workflow changes to claude.yml

Copy the jobs from `claude-yml-additions.yml` into `.github/workflows/claude.yml`:
- Add `sdk-review` job (after the existing `challenge` job)
- Add `reset-review-status` job (after `sdk-review`)
- Add `refactor-v3` to the `pull_request` trigger branches list

Also ensure the top-level `permissions` include:
```yaml
permissions:
  id-token: write
  contents: write      # was 'read' — needs write for pushing fixes
  issues: write
  pull-requests: write
  statuses: write      # NEW — needed for commit status API
```

## 2. Add branch-keeper.yml

Copy `branch-keeper.yml` to `.github/workflows/branch-keeper.yml`.

## 3. Configure Branch Protection (Required)

Go to: **Settings → Branches → Branch protection rules**

### For `refactor-v3` (now):

1. Click "Add rule" (or edit existing rule for `refactor-v3`)
2. Branch name pattern: `refactor-v3`
3. Enable: **Require status checks to pass before merging**
4. Search for and add: `sdk-review`
5. Do NOT enable "Require branches to be up to date before merging"
   (branch-keeper handles this for auto-maintained PRs; for others,
   authors update manually and the status check persists)
6. Save changes

### For `main` (later, after refactor-v3 merges):

Same as above but for branch pattern `main`. Also update
`branch-keeper.yml` to trigger on `main` instead of `refactor-v3`.

## 4. Verify Secrets

All secrets should already exist. Verify in **Settings → Secrets and variables → Actions**:

| Secret | Required | Notes |
|--------|----------|-------|
| `LITELLM_API_KEY` | Yes | Routes both Claude and GPT models via llmproxy.atlan.dev |
| `GLOBALPROTECT_USERNAME` | Yes | VPN access |
| `GLOBALPROTECT_PASSWORD` | Yes | VPN access |

No new secrets needed. `GITHUB_TOKEN` is automatic.

## 5. Create Labels (Optional — skill auto-creates them)

The skill creates labels on first use, but you can pre-create them:

```bash
gh label create "sdk-review-approved" --color "0e8a16" --description "PR passed SDK review" --force
gh label create "needs-human-review" --color "d93f0b" --description "SDK review needs human decision" --force
gh label create "needs-rebase" --color "e4e669" --description "PR has conflicts needing manual rebase" --force
```

## 6. Test the Setup

1. Create a test PR against `refactor-v3`
2. Comment: `@sdk-review`
3. Verify:
   - Status check appears as "pending" immediately
   - Review runs (6 Opus agents + GPT-5.3-codex adversarial)
   - Summary comment posted with findings
   - Inline comments posted on specific lines
   - Status check set to pass/fail based on verdict
   - Merge button is blocked if review has Critical findings

4. Test auto-fix: Comment `@sdk-review please resolve all issues`
5. Test override: Comment `@sdk-review override: testing the override mechanism`
   (must be repo admin)

## Usage Summary

| Action | Command |
|--------|---------|
| Review a PR | Comment `@sdk-review` on the PR |
| Review + auto-fix | Comment `@sdk-review please resolve all issues` |
| Override a blocked review | Comment `@sdk-review override: <reason>` (admin only) |
| Dispute a finding | Comment `@sdk-review` with explanation of why a finding is wrong |
