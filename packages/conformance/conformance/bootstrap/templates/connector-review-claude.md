<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->
## Before opening a pull request (mandatory)

### 1. Conformance

Fix every finding your change introduces, blocking and warning alike, before
you open or update the pull request:

```bash
git fetch origin main
CONFORMANCE_SINCE=origin/main uv run atlan-application-sdk-conformance detect \
  --repo . --output /tmp/conformance.sarif
```

- `[INTRODUCED]` lines (exit 1) are blocking findings you added. Fix them and
  re-run until it exits 0. Each rule's guidance is in the output and the SARIF.
- Warnings are not compared with the base. To find the ones you added, run
  the same `detect` on a `git worktree add /tmp/conformance-base origin/main`
  checkout (`--output /tmp/conformance-base.sarif`, exit code ignored, then
  `git worktree remove` it). A rule and file with more `[WARN]` lines on your
  branch is yours: fix it.
- `falling back to the whole-repository verdict`: the base could not be
  compared. Fix the cause it names (`git fetch origin main`, or
  `git fetch --unshallow`), not findings you did not add. If it still cannot
  compare, say so in the pull request.
- `[PARSE]`: fix it if your change broke the file.
- Inherited findings and warnings you did not add are not yours: the
  remediation lane owns them. An older conformance package ignores
  `CONFORMANCE_SINCE`; if it fails only on findings you did not add, say so
  in the pull request.

**Fix, do not suppress.** Fix the finding in this repository. If the rule is
wrong for this code, open a rule-fix pull request in application-sdk and link
it. Only a deliberate, genuine exception that neither fix can reach, such as a
legacy name kept for compatibility, may be suppressed, and only as
`# conformance: ignore[<RULE>] <reason>` on that one line, named in the pull
request. Never suppress another way: no exclude path or ignore entry in the
conformance, ruff, pyright or pre-commit configuration; no `# noqa`,
`# type: ignore` or `# pyright: ignore` on code you added; no skipped, xfailed,
deleted or weakened test; no lowered threshold or coverage floor; no
`--no-verify` or skipped hook. Leave suppressions already on `main` alone
unless the task is to fix them.

### 2. Principles review

Check your diff (`git diff origin/main...HEAD`) the way the connector reviewer
will, against this repository's `rp*.md` in
`.github/connector-reviewer/principles/` and the fleet `fp*.md` in
[application-sdk](https://github.com/atlanhq/application-sdk/tree/main/.github/connector-reviewer/principles).
For each principle whose `applies-to` covers a changed file (no `applies-to`
means every file), answer its `Ask:` questions, skip its `Don't flag:` cases
and grade by its `Severity:` line. Fix what you judge valid; the reviewer
judges the rest on the pull request. Never weaken or reword a principle to
pass: a principle change is its own pull request.

### 3. Validate with unit and integration tests, never e2e (mandatory)

An agent validates a change with this repository's unit and integration
tests only: run them locally before pushing, and rely on the pull request's
`tests` checks after. That is the whole validation bar for an agent, before
or after the pull request is opened. There is no exception.

An agent never starts an e2e run, for any reason:

- never comment `/e2e` or `/gate-check` on a pull request;
- never dispatch the `tests` workflow with `run_e2e` set, or any other e2e
  workflow, through `gh workflow run`, the API or any other route;
- never add a label, empty commit or push meant to start one.

e2e runs against a live tenant and gates releases; it is not a check on an
agent's change. If the unit or integration tests are not enough to prove the
change, add or fix those tests, or say what they cannot cover in the pull
request. Starting e2e, including `/gate-check` on a release, is a person's
decision and is done by that person.

## After opening a pull request: `/review`

The connector reviewer judges a change against the principles, not a rule
list, and does not repeat CI. It runs only when asked, by a comment that
starts with one of these commands (any other word on that line gets a usage
reply and nothing runs):

- `/review`: review the pull request. Comment it again after each push; only
  the new commits are reviewed.
- `/review force`: review again even if this head was reviewed or the pull
  request is exempt, and redo the approach check. Add `verify-budget` only
  when the summary suggests it.
- `/review dismiss F-xxxxxx <reason>`: close a wrong finding, with the real
  reason, never to get green. The author cannot dismiss a high or critical
  finding.
- `/review recheck F-xxxxxx <why>`: judge one finding again against the code.
- `/review waive <reason>`: set the review aside for this head only, when the
  reviewer is unavailable.

After `dismiss`, `recheck` or `waive`, the following lines are part of the
reason. Under a plain `/review`, the next line is a note for people; do not
start it with `dismiss`, `recheck` or `waive`.

What comes back:

- One summary comment and a `connector-reviewer` status. It is green only
  when every finding, of any severity, is fixed, dismissed or rechecked, or
  the head is waived (findings stay open; the next push needs a review or a
  waive again).
- Each finding cites a principle. Critical and high findings block merge. A
  "⚖️ contested" finding was disputed by a second check and is now medium:
  read both reasons, then fix or dismiss it. The approach check is advice.
- Exempt (reported, not reviewed): Renovate and Dependabot, bootstrap resync,
  version bumps (they still need `/gate-check`), and Markdown or `docs/`-only
  changes, unless they touch `.github/connector-reviewer/`, `CLAUDE.md`,
  `AGENTS.md` or `.claude/`.

This repository's principles are `rpNN-<slug>.md` files in
`.github/connector-reviewer/principles/`, which belongs to this repository
(add one by its README; it applies once merged). Every other
bootstrap-managed file under `.github/` changes in application-sdk and
arrives by resync.

### What an agent may do

Run conformance and the principles review, push fixes, comment `/review` after
a push, and comment `/review recheck` with evidence when it believes a finding
is wrong. Dismissing and waiving are a person's decision: post them only with
that person's reason. Use `/review force` only when a person asks. Never
start e2e (`/e2e`, `/gate-check` or a dispatch): see section 3.

The repository's CI configuration remains authoritative for enforcement.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
