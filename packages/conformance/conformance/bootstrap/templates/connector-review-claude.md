<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->
## Mandatory pre-commit review (L2-L4 rules; L1 before the PR and in PR CI)

Before any `git commit`, run the `connector-review` skill for the applicable
L2-L4 rules. It is local review guidance, not a marker-based commit gate.

Shared L2/L4 rules are fetched into `.mothership/.cache/review-rulesets/` and
L3 rules live in `.mothership/review-rulesets/connector-app/`.

## Mandatory before opening a pull request: L1 conformance on your change

L1 is the conformance suite. PR CI grades a pull request on what it changes:
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

The repository's CI configuration remains authoritative for enforcement.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
