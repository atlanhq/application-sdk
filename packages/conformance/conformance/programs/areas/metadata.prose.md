---
kind: responsibility
name: metadata-area
description: >
  Maintains the current M-series (customer-facing metadata) violation-set and
  drives SUGGEST-ONLY remediation: for each finding the model drafts a proposed
  rewrite, but the proposal is recorded for human review and never auto-applied.
  M-series findings are themselves model-detected (non-deterministic), and the
  fix is customer-facing copy that no deterministic gate can validate — so a
  model detecting, model fixing, and model verifying its own edit would be pure
  self-judging (design §6.1). The human is the gate.
---

### Maintains

The current set of unsuppressed M-series (customer-facing metadata) conformance
findings in the working tree, as reported by `suite.runner --series M`, each
paired with a model-drafted **proposed** rewrite for human review.

#### violations-metadata

The fingerprint-set of all unsuppressed M-series results.  Both M-rules ship at
WARN, so in default mode this covers WARNING results; strict mode is identical
(there are no BLOCK-tier M rules — a model verdict never blocks).

Postcondition (suggest-only — the loop proposes but does not apply):

> Every M-series finding routes to the residue report with a drafted rewrite
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it, or accepts the current copy via a
> `# conformance: ignore[Mxxx]` directive) manually.  The deterministic gate is
> unaffected — the M-series is not part of the arbiter command, and only humans
> clear M findings.

**Why suggest-only, not auto-applied (not an oversight):** the M-series is the
first *model-driven detection* series (BLDX-1575).  Two properties force
suggest-only:

1. **No orthogonal gate.**  An M-fix rewrites customer-facing copy (a
   description or a name).  `recheck-narrowest` re-runs `--series M`, but that
   re-check is *the same kind of model verdict* that produced the finding — it
   is not an independent, deterministic arbiter the way the test suite is for a
   code fix.  Confirming "the jargon is now gone" by asking the model again is
   the model judging its own edit.  Per design §6.1, a fix no orthogonal gate
   can validate must not be auto-applied.

2. **Editorial judgment.**  Product/marketing wording is a human decision; the
   model can draft a cleaner phrasing but must not silently rewrite what ships
   to customers.

The safe form is **propose, don't apply**: the model drafts a concrete rewrite,
a human is the gate.  Some M findings are also legitimately *not* changes at all
— a name or phrase may be deliberate — where a justified inline suppression is
the correct disposition, again a human call.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"` (identical behaviour here).

### Continuity

Input-driven: re-render when `atlan.yaml` under `scope` changes.

### Execution

```prose
# Suggest-only: detect, draft a rewrite per finding, route to residue WITHOUT
# applying.  No orthogonal gate can validate an M-series fix, so the human is
# the gate — this area never mutates the working tree.
let violations = call detect-violations
  scope: scope
  series: "M"
  target: "failing+warning"

for each finding in violations:
  let proposal = call remediate-finding
    finding: finding
    mode: mode

  # The proposal is recorded, never applied.  classification is always
  # "judgment" for M-series, so it lands in the human-review residue.
  add { finding, proposal } to residue with note "M-series suggest-only: proposed rewrite drafted for human review; NOT applied (model-detected finding + customer-facing copy; no orthogonal gate can validate the rewrite without the model judging its own edit)"
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "metadata"`._

Drafts a **proposed** rewrite for human review.  This area is suggest-only: the
proposal is recorded in residue and **never applied** — see **Why suggest-only**
above.  `classification` is always `"judgment"` for both M-rules.

The finding carries the flagged span in `atlan/evidence` (and in the finding
`message`).  Use it to target the smallest edit:

- **M001 AppNamingConvention** — the customer-facing `name` / `display_name` in
  `atlan.yaml` embeds a vendor/company prefix the fleet convention omits.  Draft
  a rewrite that names the app after the system/technology it integrates with
  (e.g. `Amazon Redshift Miner` → `Redshift Miner`).  Note that the authoritative
  name lives in `contract/app.pkl` and is generated into `atlan.yaml` — the
  proposal must point the human at the `.pkl` source (never hand-edit
  `atlan.yaml`; C002/K-series catch stale generated artifacts), and a name change
  may also need the code-side `name = "..."` and `.env.example` updates that P025
  governs.  Flag that breadth in the proposal.

- **M002 InternalJargonInDescription** — the `short_description` /
  `long_description` / entrypoint `description` contains internal jargon,
  codenames, or non-customer-appropriate wording (the flagged span is in
  `evidence`).  Draft a customer-neutral rewrite of just that span, preserving
  the factual meaning.  As with M001, descriptions are generated from
  `contract/app.pkl` (`shortDescription` / `longDescription` / entrypoint
  `description`) — the proposal must edit the `.pkl` source and re-generate, not
  `atlan.yaml` directly.

Set `not_remediable = false` (a rewrite can always be drafted).  Set
`external_influence = false` — no external lookup is involved.  Never write to
the tree from this area.

### Write-scope

This area writes **nothing** to the working tree (suggest-only).  It may read
`atlan.yaml` and `contract/**/*.pkl` to draft a proposal, but the proposal is
recorded in residue for a human to apply.  The general write-scope constraint
(`remediate-finding` — no writes to `tests/`, `.github/`, `conformance/`) applies
unchanged.
