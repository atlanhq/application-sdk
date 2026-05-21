# Auto-Complete Mode

Selected when Intent Inference (ORCHESTRATION.md Phase 0 §Intent
Inference) picks Mode C — i.e., `COMMENTER_INTENT` contains phrases
like `auto-complete`, `resolve all`, `apply fixes`, `fix it`, `fix the
issues`. After posting the initial review (Phase 3), enter the in-
sandbox fix loop (Phase 4).

## Fix Loop

Source of findings: parse the just-posted SDK Review comment's
`<!-- REVIEW_DATA -->` JSON block. That is the authoritative list — do
NOT read findings from any `session/` path.

For each finding with scope PATCH (any severity — Critical, Important, Minor):

1. Read the full file for context — understand WHY this fix is needed
2. Apply the exact fix from the finding's `suggested_fix` or `<!--FIX-->` block
3. Run: `uv run pre-commit run --files <changed_files>`
   - If pre-commit fails, fix the lint/format issues and re-run
4. Run: `uv run pytest tests/unit/ -x --timeout=60`
   - If tests fail, revert this specific fix and note why
5. Verify only intended files changed: `git diff --stat`
   - If unrelated files were reformatted by pre-commit, discard them:
     `git checkout -- <unrelated_files>`

After all PATCH fixes are applied:

6. Stage specific files: `git add <specific_files>`
   - NEVER use `git add -A` or `git add .`
7. Commit: `git commit -m "fix(review): address SDK review findings"`
   - NEVER add Co-Authored-By lines
8. Push: `git push origin <branch>`

Then re-run Phases 0-3 with the new diff to verify fixes.

## Iteration Limits

- Maximum 3 iterations total
- If iteration >= 3 and findings remain: submit with `approval_recommendation: "REQUEST_CHANGES"` and note "Max iterations reached. N findings remain."
- If CI fails after push: attempt one fix iteration for the CI failure. If CI fails again: stop, submit `REQUEST_CHANGES` with "CI failing after 2 consecutive fix attempts."

## Scope Restrictions

- ONLY fix PATCH scope findings
- NEVER touch MIGRATE, REFACTOR, or DESIGN_CHANGE scope findings
- NEVER modify files the PR doesn't already touch
- NEVER add features, refactor, or "improve" things beyond the findings
- NEVER force-push

## Reading Author Instructions

The free-form text the human typed after `@sdk-review` is forwarded to
the orchestration as `COMMENTER_INTENT`. The author may have included
specific guidance like "skip the performance findings" or "focus on
security". Honor these instructions unless they conflict with
guardrails G1-G5 (those are non-negotiable).

## When to Stop

Stop the fix loop and submit the current state when:
- All PATCH findings are resolved -> submit APPROVE
- iteration >= 3 -> submit REQUEST_CHANGES
- CI fails twice consecutively -> submit REQUEST_CHANGES
- `git push` fails (branch updated externally) -> submit REQUEST_CHANGES with "Branch updated externally. Re-run @sdk-review."
- Only MIGRATE/REFACTOR/DESIGN_CHANGE findings remain -> submit CONDITIONAL_APPROVE with "Remaining findings need human decisions."
