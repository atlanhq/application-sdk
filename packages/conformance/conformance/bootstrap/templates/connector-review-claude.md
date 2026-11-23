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

Every pull request that changes behaviour is reviewed by the connector reviewer.
It judges the change against principles, not a rule list, and it does not
repeat what conformance or CI already checks.

- Ask for a review by commenting `/review` on the pull request. It reviews only
  when asked. After a new push, comment `/review` again: only the new commits
  are reviewed.
- The result is one summary comment and a `connector-reviewer` status on the
  head commit.
- Read the principles before changing code they cover. The fleet principles are
  in application-sdk at `.github/connector-reviewer/principles/`
  (https://github.com/atlanhq/application-sdk/tree/main/.github/connector-reviewer/principles).
  This repository's own are in `.github/connector-reviewer/principles/`.
- Fix each finding. If a finding is wrong, close it with
  `/review dismiss F-xxxxxx <reason>` and give the real reason. Never dismiss a
  finding to get a green result. The author of a pull request cannot dismiss a
  high or critical finding on it.
- `/review recheck F-xxxxxx <what changed, or why it is wrong>` asks the
  reviewer to look at one finding again.
- The approach check at the top of the summary is advice. Act on it or leave it.
- No review is needed for: dependency bumps from Renovate, bootstrap resync pull
  requests, version-bump pull requests (those need `/gate-check`), and
  docs-only changes that do not touch the principle files, `CLAUDE.md`,
  `AGENTS.md` or `.claude/`.
- If the reviewer is unavailable, anyone with write access can comment
  `/review waive <reason>`. The reason is recorded on the pull request.
- Do not edit bootstrap-managed files under `.github/` in this repository.
  Change them in application-sdk; a resync brings them here.

The repository's CI configuration remains authoritative for enforcement.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
