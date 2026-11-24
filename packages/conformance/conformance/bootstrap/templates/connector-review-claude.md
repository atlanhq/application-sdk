<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->
## Mandatory before opening a pull request: conformance on your change

PR CI runs the conformance suite and grades a pull request on what it changes:
it fails on a blocking finding the change adds and not on one `main` already
carries. Run the same check locally and get it green before you open or
update a pull request, so CI is never where you learn of it:

```bash
git fetch origin main
CONFORMANCE_SINCE=origin/main uv run atlan-application-sdk-conformance detect \
  --repo . --output /tmp/conformance.sarif
```

- Exit 0: nothing blocking was introduced. Open the pull request.
- Exit 1 with `[INTRODUCED]` lines: each is a blocking finding your change
  added. Fix the code and re-run until it exits 0. The rule's guidance is in
  the output and in `/tmp/conformance.sarif`.
- Exit 1 with `falling back to the whole-repository verdict` and no
  `[INTRODUCED]` line: the base could not be compared, so the run graded the
  whole repository. Do not start fixing findings you did not add. Fix the
  cause the message names (usually `git fetch origin main`, or a shallow
  clone: `git fetch --unshallow`) and re-run. If it still cannot compare,
  stop and say so in the pull request.
- Exit 1 with a `[PARSE]` line: a file could not be parsed. That fails the
  run whoever broke it; fix it if your change did.
- Do not silence a finding with `# conformance: ignore[...]`, an exclude
  path, or a test or threshold change. If you believe the rule is wrong for
  this code, stop and say so in the pull request instead of suppressing it.
- Findings reported as inherited from the base, and warnings, do not fail
  the run. Leave them alone unless the task is to fix them: the scheduled
  conformance run and its remediation lane own that backlog.
- An older conformance package ignores `CONFORMANCE_SINCE` and grades the
  whole repository. That verdict is stricter, never laxer; if it fails only
  on findings you did not add, say so in the pull request.

## After opening a pull request: the connector reviewer

Every pull request that changes behaviour should be reviewed by the connector
reviewer. It judges the change against principles, not a rule list, and it does
not repeat what conformance or CI already checks. It reviews only when asked.

Ask by commenting on the pull request. The command must start the comment, and
every word on its line must be part of one of the commands below. Any other
word gets a usage reply and nothing runs. To add a note for the people reading
the pull request, put it on the next line; the reviewer does not read it. Do
not start that note with `dismiss`, `recheck` or `waive`.

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
  dismissed or resolved by a recheck.
- Each finding names the principle it cites. A critical or high finding blocks
  merge.
- A finding marked "⚖️ contested" was raised as high, but a second, independent
  check found evidence in the code against it. It is now medium, so it does not
  block merge and the author may dismiss it. Read both reasons, then fix it or
  dismiss it.
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

An agent working on a pull request may run conformance, push fixes, comment
`/review` after a push, fix the findings, and comment `/review recheck` with
the evidence when it believes a finding is wrong.

Dismissing a finding and waiving the review are decisions for a person. Post
`/review dismiss` or `/review waive` only when a person has made that decision
and given the reason, and use their reason. Use `/review force` only when a
person asks for it.

The repository's CI configuration remains authoritative for enforcement.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
