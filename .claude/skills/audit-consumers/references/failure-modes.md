# Phase E failure modes, manifest schema, and end-of-phase summary

---

## `manifest.json` schema

Written to `/tmp/audit-fix/<spec-slug>/manifest.json` at Phase E0 and updated after each
repo's terminal state transition. Provides crash-recovery state — re-running Phase E reads
this file and skips repos already in a terminal state.

```json
{
  "spec_slug": "sql-error-types",
  "branch_name": "sdk-audit/sql-error-types",
  "target_release": "application_sdk#1234",
  "started_at": "2026-05-15T14:02:00Z",
  "auto_confirm_remaining": false,
  "repos": [
    {
      "repo": "atlanhq/foo",
      "state": "pr_open",
      "pr_url": "https://github.com/atlanhq/foo/pull/42",
      "branch": "sdk-audit/sql-error-types",
      "hits_patched": 3,
      "hits_unresolved": 1,
      "error": null
    },
    {
      "repo": "atlanhq/bar",
      "state": "failed",
      "pr_url": null,
      "branch": "sdk-audit/sql-error-types",
      "hits_patched": 0,
      "hits_unresolved": 0,
      "error": "py_compile failed: clients/bar.py:42: SyntaxError: invalid syntax"
    }
  ]
}
```

**State machine** — Phase E writes only three terminal states plus the initial `pending`:

```
pending → pr_open   (E7: PR created successfully)
        → skipped   (E1: archived or no push permission; E2: open PR already exists;
                     E6: user answered "skip" or --dry-run)
        → failed    (E3: clone failed; E5: py_compile failed;
                     E7: push rejected or gh pr create failed)
```

Intermediate steps (clone, patch, verify) are not persisted as manifest states; only the
terminal outcome is recorded. The manifest entry for a repo is written once, at the moment
the repo reaches a terminal state.

---

## Failure-mode table

| Failure | User-facing chat line | State | Recovery |
|---|---|---|---|
| LLM diff fails `git apply --check` | `[Phase E] atlanhq/<repo>: LLM diff for <file>:<line> rejected (does not apply). Listed as TODO in PR body.` | hit → unresolved; repo continues | Reviewer edits the opened PR |
| LLM diff touches out-of-scope file | `[Phase E] atlanhq/<repo>: LLM diff for <file>:<line> rejected (modifies out-of-scope file). Listed as TODO.` | hit → unresolved; repo continues | Reviewer edits the opened PR |
| LLM diff exceeds scope-creep threshold | `[Phase E] atlanhq/<repo>: LLM diff for <file>:<line> rejected (scope creep: N lines). Listed as TODO.` | hit → unresolved; repo continues | Reviewer edits the opened PR |
| LLM returns `ABSTAIN` | `[Phase E] atlanhq/<repo>: LLM abstained on <file>:<line> (R<N>). Listed as TODO in PR body.` | hit → unresolved; repo continues | Reviewer edits the opened PR |
| `py_compile` fails on patched file | `[Phase E] atlanhq/<repo>: py_compile FAILED on <file>:\n<stderr>\nRolling back this repo — no PR will be opened.` | repo → failed; rollback via `git checkout origin/<default_branch> -- <file>` | Re-run with a refined spec or open manually from the report |
| `ruff check` fails | `[Phase E] atlanhq/<repo>: ruff WARN — proceeding. See PR body Verification section.` | repo → verified (with warnings) | Reviewer resolves during PR review |
| `gh auth status` fails (Phase E0) | `[Phase E] gh not authenticated. Run: gh auth login --scopes repo\nSkipping Phase E — report has already been written.` | Phase E exits cleanly | User re-authenticates, re-runs with `--raise-prs` |
| `gh api` returns no push permission | `[Phase E] atlanhq/<repo>: SKIP — no push permission for <gh-user>. Open this PR manually from the per-repo section of the audit report.` | repo → skipped | Manual PR from the audit report |
| Repo is archived | `[Phase E] atlanhq/<repo>: SKIP — repo archived.` | repo → skipped | None; archived repos are not migration targets |
| `git clone` fails | `[Phase E] atlanhq/<repo>: clone failed:\n<stderr>` | repo → failed | Check SSH key / network, re-run |
| `git push` rejected | `[Phase E] atlanhq/<repo>: push rejected:\n<stderr>\nLocal branch preserved at /tmp/audit-fix/<slug>/<repo> for manual inspection.` | repo → failed (branch exists locally) | `cd /tmp/audit-fix/<slug>/<repo> && git push` after resolving |
| `gh pr create` fails | `[Phase E] atlanhq/<repo>: PR creation FAILED:\n<stderr>\nBranch was pushed — open PR at: https://github.com/atlanhq/<repo>/compare/sdk-audit/<slug>` | repo → failed (branch is up at remote) | User opens PR via the compare URL |
| User answers `skip` at E6 prompt | `[Phase E] atlanhq/<repo>: skipped by user. Local clone discarded.` | repo → skipped | — |
| Ctrl-C mid-flight | (SIGINT — no cleanup) | manifest reflects last committed state | Re-run; `pr_open` repos skipped by E2; `pending` repos retried |
| GPG signing required by branch protection | `[Phase E] atlanhq/<repo>: push rejected (signature required).\n  Tip: git config commit.gpgsign true` | repo → failed | User enables signing, re-runs |

**Cleanup reminder** (printed in the end-of-phase summary):
```
To clean up workspaces: rm -rf /tmp/audit-fix/<spec-slug>/
```

Do NOT auto-delete — the engineer may want to inspect failed repos.

---

## End-of-phase summary (chat output)

```
═════════════════════════════════════════════════════════
Phase E complete — migration PR summary
═════════════════════════════════════════════════════════

Spec:            <spec title>
Target release:  <spec.target_release>
Branch:          sdk-audit/<spec-slug>
Report:          <report-path>

| Repo             | State        | PR                    | Patched | Unresolved |
|---|---|---|---|---|
| atlanhq/foo      | pr_open      | <url>                 | 4       | 1          |
| atlanhq/bar      | pr_open      | <url>                 | 2       | 0          |
| atlanhq/baz      | skipped      | —                     | —       | —          | (no push permission)
| atlanhq/qux      | failed       | —                     | —       | —          | (py_compile: clients/qux.py:42)
| atlanhq/quux     | pr_open      | <existing-url>        | 0       | 0          | (PR already open and current — skipped)

Totals: 3 succeeded · 1 skipped · 1 failed · 1 already-open

Next steps:
  1. Review each PR — auto-fixes are best-effort; verify diff + TODOs.
  2. For skipped/failed repos, see /tmp/audit-fix/<spec-slug>/manifest.json and the
     per-repo section of the audit report.
  3. After all PRs merge, re-run /audit-consumers (without --raise-prs) to confirm
     zero findings remain.

Workspace cleanup: rm -rf /tmp/audit-fix/<spec-slug>/
```
