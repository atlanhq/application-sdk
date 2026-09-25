---
kind: function
name: report-rule-defect
description: >
  Turns a finding that cannot be fixed honestly — a false positive, or a
  prescription that cannot clear what the checker flags — into a reviewed
  change proposal against the conformance suite in atlanhq/application-sdk:
  a regression test that reproduces the defect, the fix when it is local, and
  a pull request the SDK owners review.  The lane never merges it, and never
  edits the gate it is judged against inside the app under scan.
---

### Parameters

- `finding` (object, required) — the finding as returned by `detect-violations`
  (`rule_id`, `file`, `line`, `fingerprint`, `message`, `hint`,
  `canonical_reference`).
- `kind` (string, required) — `"false-positive"` or `"prescription-defect"`
  (`remediate-finding` step 5 decides which).
- `evidence` (object, required):
  - `snippet` — the flagged lines with enough context to reproduce, at most 40
    lines, **secret values redacted** (names of env vars and secret-store paths
    may stay, values never).
  - `reference` — the `canonical_reference` file and the lines in it that show
    the same shape passing.
  - `attempts` — for a prescription defect: each edit that was applied, what
    the recheck and the orthogonal gate said, in order.
- `app_repo` (string, required) — the repository *name* under scan (e.g.
  `atlan-mysql-app`); never a tenant, customer or run identifier.
- `connector_issue` (string, optional) — the Linear identifier of the
  connector's issue, so the PR links back to the burn-down.

### Returns

- `pr_url` — the pull request URL, or `null` when none could be opened.
- `existing` — `true` when an open PR for the same rule and kind already
  existed and was reused.
- `draft` — when no PR could be opened (see guard-rails), the full PR title and
  body as a string, so a human can open it; otherwise `null`.

### Procedure

1. **Dedup first.**  `gh pr list -R atlanhq/application-sdk --state open
   --search "<RULE_ID> in:title"`.  If an open PR names the same rule and the
   same kind, return it as `existing = true` and, only if this reproducer
   differs from what the PR already carries, add the new snippet as a comment.
   One PR per rule per kind; further evidence is batched as comments.
2. **Clone the suite.**  Shallow-clone `atlanhq/application-sdk` into
   `remediation/sdk/` (scratch — never in `touched_files`, never committed to
   the app) and branch `conformance/<rule-id-lower>-<kind>-<short-slug>`.
3. **Reproducer before fix.**  Add a pytest under
   `packages/conformance/tests/` that builds a fixture from `evidence.snippet`,
   runs the shipped checker for `finding.rule_id` on it, and asserts the
   expected behaviour: **no finding** for a false positive; for a prescription
   defect, that applying the prescribed edit to the fixture leaves zero
   findings for the rule.  Run it from `packages/conformance` with
   `uv sync --all-extras --all-groups && uv run pytest <that file>`; it must
   **fail on `main`** — a reproducer that passes proves nothing.
4. **Fix when local.**  If the false positive is a predicate in
   `suite/checks/<checker>.py`, or the defect is the prescription text in
   `programs/areas/<area>.prose.md`, and the correction is small and obviously
   right, make it in the same PR: run the whole package suite, and
   `atlan-application-sdk-conformance gen-rule-docs` if a rule definition
   changed.  Otherwise ship the reproducer alone, marked
   `@pytest.mark.xfail(strict=True, reason="<RULE_ID> <kind>: see PR")` — a
   strict xfail keeps CI green today and turns red the day the checker is
   fixed, so the reproducer cannot be forgotten.
5. **Open the PR.**  Title `fix(conformance): <RULE_ID> false positive on
   <pattern>` or `fix(conformance): <RULE_ID> prescription cannot clear
   <pattern>`.  Body: the rule, the kind, the redacted snippet, the
   reference-app file it matches, what was attempted (prescription defects),
   the SARIF fingerprint, `app_repo` and `connector_issue`, and one closing
   sentence that the remediation lane opened this and it needs review by the
   SDK owners.  No customer or tenant names, no run ids, no secret values.
6. **Return** `pr_url`.  The finding in the app stays open, or — WARN-tier
   only — is suppressed with a justification citing the PR, exactly as
   `remediate-finding` step 5 prescribes.

### Guard-rails

- **Never merge, approve or enable auto-merge** on the PR; never edit the
  app's own `conformance/` directory or its vendored detect action.  This is
  the §6.1 "no self-judging changes" discipline applied across repositories:
  the remediator may *propose* a change to the gate, only humans accept it.
- If `gh` is not authenticated for `atlanhq/application-sdk` (the lane's token
  is scoped to the app repos), do not drop the signal: return `pr_url = null`
  and the complete title + body as `draft`, and `detect-fix-recheck` carries it
  into residue as `rule_defect_draft` for a human to open.
- The reproducer fixture is built from the snippet with identifiers
  generalised (`my_conn` not the customer's name, `example.internal` not a
  real host) whenever the original would identify a tenant.
