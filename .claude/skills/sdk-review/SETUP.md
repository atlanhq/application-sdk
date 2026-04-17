# SDK Review v2 — Setup Instructions

## ✅ Workflows Already Deployed

This PR ships the workflows directly into `.github/workflows/`:
- `.github/workflows/claude.yml` — extended with `sdk-review` and `reset-review-status` jobs
- `.github/workflows/branch-keeper.yml` — new workflow for post-approval branch maintenance

No manual workflow installation needed. Once this PR merges, the system is live.

## ⚠️ One-Time Manual Step: Branch Protection (Required)

To make `sdk-review` a required check that blocks merge, configure branch protection:

Go to: **Settings → Branches → Branch protection rules**

### For `main`:

1. Click "Add rule" (or edit existing rule for `main`)
2. Branch name pattern: `main`
3. Enable: **Require status checks to pass before merging**
4. Search for and add: `sdk-review`
5. Do NOT enable "Require branches to be up to date before merging"
   (branch-keeper handles this for auto-maintained PRs; for others,
   authors update manually and the status check persists)
6. Save changes

## Secrets — All Already Configured

| Secret | Status | Notes |
|--------|--------|-------|
| `LITELLM_API_KEY` | ✅ Already exists | Routes both Claude and GPT models via `llmproxy.atlan.dev` |
| `GLOBALPROTECT_USERNAME` | ✅ Already exists | VPN access |
| `GLOBALPROTECT_PASSWORD` | ✅ Already exists | VPN access |
| `GITHUB_TOKEN` | ✅ Auto-provided | No action needed |

**Nothing new to add.**

## Optional: Pre-Create Labels

The skill auto-creates labels on first use, but you can pre-create them:

```bash
gh label create "sdk-review-approved" --color "0e8a16" --description "PR passed SDK review" --force
gh label create "sdk-review-auto-maintained" --color "1d76db" --description "Bot keeps this PR up-to-date with base" --force
gh label create "needs-human-review" --color "d93f0b" --description "SDK review needs human decision" --force
gh label create "needs-rebase" --color "e4e669" --description "PR has conflicts needing manual rebase" --force
```

## Test the Setup (After Merge)

1. Create a test PR against `main`
2. Comment: `@sdk-review`
3. Verify:
   - Status check `sdk-review` appears as "pending" immediately
   - Review runs (3 Opus agents + GPT-5.3-codex adversarial)
   - Summary comment posted with findings grouped by file
   - Inline comments posted on specific lines
   - Status check set to pass/fail based on verdict
   - Merge button is blocked if review has Critical findings

4. Test auto-complete: Comment `@sdk-review auto-complete`
5. Test stop (while auto-complete is running): Comment `@sdk-review stop`
6. Test override: Comment `@sdk-review override: testing the override mechanism`
   (must be repo admin)
7. Test Q&A: Reply to an inline comment with a question; the bot should respond.

## Usage Summary

| Action | Command |
|--------|---------|
| Review a PR | Comment `@sdk-review` on the PR |
| Review + auto-complete (fix loop) | Comment `@sdk-review auto-complete` |
| Cancel in-progress auto-complete | Comment `@sdk-review stop` |
| Override a blocked review | Comment `@sdk-review override: <reason>` (admin only) |
| Dispute a finding | Comment `@sdk-review` with explanation of why a finding is wrong |

**Aliases:** `resolve all issues` == `auto-complete`, `cancel` == `stop`.

**Permission:** Only repo OWNER/MEMBER/COLLABORATOR can trigger (external fork
contributors cannot). This is enforced at the workflow level via
`author_association`.
