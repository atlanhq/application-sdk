---
kind: responsibility
name: preflight-area
description: >
  Maintains the current F-series (preflight-gate) violation-set and drives
  SUGGEST-ONLY remediation: for each finding the model drafts a proposed fix,
  but the proposal is recorded for human review and never auto-applied — a
  preflight fix changes which probes block, how a failure is typed and what the
  customer is told to do, and no gate here can prove the new verdict is true
  for the real source.
---

### Maintains

The current set of unsuppressed F-series (preflight-gate) conformance findings
in the working tree, as reported by `suite.runner --series F`, each paired with
a model-drafted **proposed** fix for human review.

#### violations-preflight

The fingerprint-set of all unsuppressed FAILING F-series results.  Extends to
include WARNING results in strict mode.

Postcondition (suggest-only — the loop proposes but does not apply):

> Every F-series finding routes to the residue report with a drafted fix
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it) manually.  The deterministic
> `atlan-application-sdk-conformance detect --repo . --series F` exit code is
> therefore unchanged by this area — only humans clear F-series findings.

**Why suggest-only, not auto-applied (not an oversight):** every F-rule names
`orthogonal_gate = "tests"`, but the app's unit tests grade the handler against
mocked probes, not against the source.  A fix that types a failure, changes a
verdict from PARTIAL to READY or NOT_READY, or adds a suggested action can pass
`recheck-narrowest` and the test gate while telling the customer the wrong thing
about their source.  The only evidence that a preflight verdict is truthful is a
real-handler scenario (F016) or a human who knows the source.  Per design §6.1,
a fix no gate can validate must not be auto-applied.

F016 reports a required scenario that is not defined: missing, skipped, not
asserting the contract, or unreadable.  Conformance never runs the scenarios;
the test gate does.  The fix is a source adapter and a pytest scenario, not an
edit to the handler, so F016 is `not_remediable` here and routes to residue with
the scenario name.  F017 and F018 are retired and never fire.  F019 reports what static analysis could not resolve; it has
no fix of its own and routes to residue as an investigation pointer.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.
- `rule_ids` — optional list of exact rule IDs (propagated from the
  top-level entry). Forwarded verbatim into every runner invocation this
  area makes — the loop's detect calls and the suggest-only
  `detect-violations` calls alike — so a `--rule`-scoped run stays scoped
  here rather than silently widening to the whole series at this hop.
- `apply_unverifiable` — boolean, default `false`.  When `false`, behaviour is
  propose, never apply.  When `true`, the caller has accepted that no gate here
  can prove the new verdict is truthful for the source, and accepts the
  conditions below.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes, or any
file under `.github/workflows/`, `deploy/`, `deployment/`, `helm/` or `k8s/`
(the deployment declarations F015 reads).

### Execution

```prose
if apply_unverifiable:
  # Caller-accepted unverifiable mode.  A wrong preflight fix either blocks a
  # healthy source or lets a broken one through to extraction, in a tenant.
  # Three conditions, all mandatory:
  #   1. classification = "unverifiable" on every result — never "mechanical";
  #   2. the delivered change must be marked DRAFT with a named reviewer;
  #   3. `result.evidence` must cite the source-permission, SDK contract or
  #      guide section the fix rests on — empty evidence means the loop
  #      rejects the fix un-applied.
  call detect-fix-recheck
    scope: scope
    series: "F"
    rule_ids: rule_ids
    mode: mode
    max_attempts: 5
    classification_override: "unverifiable"
    require_cited_evidence: true
    deliver_as_draft: true

else:
  # Suggest-only: detect, draft a fix per finding, route to residue WITHOUT
  # applying.  The human is the gate — this area never mutates the working tree.
  let violations = call detect-violations
    scope: scope
    series: "F"
    rule_ids: rule_ids
    target: if mode == "strict" then "failing+warning" else "failing"

  for each finding in violations:
    let proposal = call remediate-finding
      finding: finding
      mode: mode

    add { finding, proposal } to residue with note "F-series suggest-only: proposed fix drafted for human review; NOT applied (no gate proves the preflight verdict is truthful for the source)"
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "preflight"`._

Drafts a **proposed** fix for human review.  `classification` is always
`"judgment"`.  Read the whole handler and every helper it reaches before
drafting, then follow the rule's section in the packaged
[preflight guide](../../docs/preflight-guide.md): each section states the
contract, how to investigate, the fix, and how to verify it.  The guide is the
prescription; do not invent a probe, a category, an audience or a suggested
action the source does not support.

- **F001–F015, F020** — static findings.  Draft the edit the guide's **Fix**
  paragraph describes, cite the **Verify** paragraph in `result.evidence`, and
  return `outcome = "fix"`.  Never suggest a `# conformance: ignore[F0xx]` for
  a BLOCK-tier finding; for a WARN-tier finding in strict mode a suppression is
  a valid draft only when the guide's **Investigate** paragraph names the case.
- **F016** — a required scenario is not defined.
  Set `not_remediable = true`, name the scenario from
  `conformance.preflight_testing.SCENARIOS` in the residue note, and stop.
- **F019** — analysis was unresolved.  Set `not_remediable = true` and record
  the unresolved construct so a human can decide whether to refactor toward a
  supported shape or define the F016 scenarios.  Say which of the two the
  finding admits: a value-level gap (a computed aggregation or an unresolvable
  row inside one, an expanded failure constructor, an unresolved error
  expression, a dynamic `passed`)
  clears once the F016 matrix is fully defined, so defining that matrix is a
  real remedy; a structural gap (unparsed file, unresolved
  `preflight_check`, dynamically bound callback, unresolved input contract)
  never clears that way and only a resolvable shape fixes it.

Never put a credential, a customer identifier or a real tenant value into a
proposal, a test fixture or `result.evidence`.
