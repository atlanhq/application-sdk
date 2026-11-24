<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->
## Mandatory before opening a pull request: conformance and a principles review

### 1. Conformance on your change

PR CI runs the conformance suite and grades a pull request on what it changes:
it fails on a blocking finding the change adds and not on one `main` already
carries. Run the same check locally and get it green before you open or
update a pull request, so CI is never where you learn of it. Fix every finding
your change introduces before you open the pull request, blocking and warning
alike:

```bash
git fetch origin main
CONFORMANCE_SINCE=origin/main uv run atlan-application-sdk-conformance detect \
  --repo . --output /tmp/conformance.sarif
```

- Exit 0: nothing blocking was introduced. Check for introduced warnings
  (below), then open the pull request.
- Exit 1 with `[INTRODUCED]` lines: each is a blocking finding your change
  added. Fix the code and re-run until it exits 0. The rule's guidance is in
  the output and in `/tmp/conformance.sarif`.
- Warnings never fail the run, and only blocking findings are compared with
  the base: no output line or SARIF field marks a warning as introduced. Each
  warning is printed as `[<rule>] [WARN] <file>:<line>:<column>`. To find the
  ones your change introduced, run `detect` on `origin/main` too, and ignore
  that run's exit code:

  ```bash
  git worktree add /tmp/conformance-base origin/main
  uv run atlan-application-sdk-conformance detect \
    --repo /tmp/conformance-base --output /tmp/conformance-base.sarif
  git worktree remove /tmp/conformance-base
  ```

  Compare its `[WARN]` lines with your branch's. A rule and file with more
  warnings on your branch than on `origin/main` is one your change
  introduced. Fix those too.
- Exit 1 with `falling back to the whole-repository verdict` and no
  `[INTRODUCED]` line: the base could not be compared, so the run graded the
  whole repository. Do not start fixing findings you did not add. Fix the
  cause the message names (usually `git fetch origin main`, or a shallow
  clone: `git fetch --unshallow`) and re-run. If it still cannot compare,
  stop and say so in the pull request.
- Exit 1 with a `[PARSE]` line: a file could not be parsed. That fails the
  run whoever broke it; fix it if your change did.
- Do not suppress a finding, in any form, to get a green run. That covers
  `# conformance: ignore[...]`; a new exclude path or ignore entry in the
  conformance, ruff, pyright or pre-commit configuration; `# noqa`,
  `# type: ignore` or `# pyright: ignore` on code your change added; a test
  that is skipped, marked xfail, deleted or weakened; a lowered threshold or
  coverage floor; and `git commit --no-verify` or a skipped hook. If you
  believe the rule is wrong for this code, stop and say so in the pull
  request instead of suppressing it.
- A suppression already on `main` is not yours to remove unless the task is
  to fix it. Never add one to make your own change pass.
- Blocking findings reported as inherited from the base, and warnings your
  change did not introduce, do not fail the run. Leave them alone unless the
  task is to fix them: the scheduled conformance run and its remediation lane
  own that backlog.
- An older conformance package ignores `CONFORMANCE_SINCE` and grades the
  whole repository. That verdict is stricter, never laxer; if it fails only
  on findings you did not add, say so in the pull request.

### 2. Review your change against the principles

The connector reviewer judges every pull request against two sets of
principles. Check your change against both before you open the pull request,
the same way it will:

- this repository's principles: every `rp*.md` in
  `.github/connector-reviewer/principles/`;
- the fleet principles: every `fp*.md` in application-sdk's
  `.github/connector-reviewer/principles/` on `main`
  (https://github.com/atlanhq/application-sdk/tree/main/.github/connector-reviewer/principles).

For each principle whose `applies-to` covers a file you changed (a principle
with no `applies-to` covers every changed file), answer its `Ask:` questions
against your diff (`git diff origin/main...HEAD`), skip what its
`Don't flag:` excludes, and judge severity by its `Severity:` line. Fix the
findings you judge valid, and leave the rest; the reviewer judges them again
once the pull request is open.

Do not weaken, delete or reword a principle so that your change passes. A
change to a principle is its own pull request, and the reviewer reviews it.

This local review does not replace `/review`. Comment `/review` once the pull
request is open.

## After opening a pull request: the connector reviewer

Every pull request that changes behaviour should be reviewed by the connector
reviewer. It judges the change against principles, not a rule list, and it does
not repeat what conformance or CI already checks. It reviews only when asked.

Ask by commenting on the pull request. The command must start the comment, and
every word on its line must be part of one of the commands below. Any other
word gets a usage reply and nothing runs. Below a plain `/review` or
`/review force`, a note on the next line is for the people reading the pull
request; the reviewer does not read it. Do not start that note with `dismiss`,
`recheck` or `waive`. After `/review dismiss`, `/review recheck` or
`/review waive`, the finding ids and the reason may continue on the lines
below, and the reviewer reads all of that text as the reason.

- `/review` reviews the pull request. After a new push, comment it again: only
  the commits since the last review are reviewed.
- `/review force` reviews again even when the head commit was already reviewed
  or the pull request is exempt, and redoes the approach check. The cost cap
  per pull request still holds. Add `verify-budget` after it only when the
  summary suggests it.
- `/review dismiss F-xxxxxx <reason>` closes a finding that is wrong. Give the
  real reason. Never dismiss a finding to get a green result. The author of a
  pull request cannot dismiss a high or critical finding on it.
- `/review recheck F-xxxxxx <what changed, or why it is wrong>` asks the
  reviewer to judge one finding again against the code.
- `/review waive <reason>` sets the review aside for the current head commit,
  for when the reviewer is unavailable. The reason is recorded on the pull
  request.

What comes back:

- One summary comment, and a `connector-reviewer` status on the head commit.
  The status is green only when every finding, of any severity, is fixed,
  dismissed or resolved by a recheck, or when the review was waived. A waive
  turns the status green for that head commit only and resolves no finding:
  they stay open, and the next commit has no green status until it is reviewed
  or waived again.
- Each finding names the principle it cites. A critical or high finding blocks
  merge.
- A finding marked "⚖️ contested" was raised as critical or high, but a second,
  independent check found evidence in the code against it. It is now medium, so
  it does not block merge and the author may dismiss it. Read both reasons, then
  fix it or dismiss it.
- The approach check at the top of the summary is advice. Act on it or leave it.
- `/review` on these pull requests reports them exempt instead of reviewing
  them: Renovate and Dependabot updates, bootstrap resync pull requests,
  version-bump pull requests (those still need `/gate-check`), and changes made
  only to Markdown or `docs/`, unless they touch
  `.github/connector-reviewer/`, `CLAUDE.md`, `AGENTS.md` or `.claude/`.

The principles:

- Read the principles before changing code they cover. The fleet principles are
  in application-sdk at `.github/connector-reviewer/principles/`
  (https://github.com/atlanhq/application-sdk/tree/main/.github/connector-reviewer/principles).
- This repository's own principles are in
  `.github/connector-reviewer/principles/`, one file each, named
  `rp01-<slug>.md`, `rp02-<slug>.md` and so on. To add one, follow the README
  in that folder. The reviewer reads them from the default branch, so a new
  principle applies once it is merged.
- That folder belongs to this repository. Bootstrap only creates its README
  when it is missing. Every other bootstrap-managed file under `.github/` is
  changed in application-sdk, not here; a resync brings the change.

### What an agent may do

An agent working on a pull request may run conformance and the principles
review, push fixes, comment `/review` after a push, fix the findings, and
comment `/review recheck` with the evidence when it believes a finding is
wrong.

Dismissing a finding and waiving the review are decisions for a person. Post
`/review dismiss` or `/review waive` only when a person has made that decision
and given the reason, and use their reason. Use `/review force` only when a
person asks for it.

The repository's CI configuration remains authoritative for enforcement.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
