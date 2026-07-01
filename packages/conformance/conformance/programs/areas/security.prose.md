---
kind: responsibility
name: security-area
description: >
  Maintains the current S-series (secret-hygiene) violation-set and drives
  SUGGEST-ONLY remediation: for each finding the model drafts a proposed fix,
  but the proposal is recorded for human review and never auto-applied —
  moving a secret to the store or onto the SDK resolution seam is a judgment
  call with no orthogonal gate that can validate it.
---

### Maintains

The current set of unsuppressed S-series (security / secret-hygiene) conformance
findings in the working tree, as reported by `suite.runner --series S`, each
paired with a model-drafted **proposed** fix for human review.

#### violations-security

The fingerprint-set of all unsuppressed FAILING S-series results.  Extends to
include WARNING results in strict mode (both S001 and S002 ship at WARN).

Postcondition (suggest-only — the loop proposes but does not apply):

> Every S-series finding routes to the residue report with a drafted fix
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it) manually.  The deterministic
> `suite.runner --series S` exit code is therefore unchanged by this area —
> only humans clear S-series findings.

**Why suggest-only, not auto-applied (not an oversight):** neither S-rule has an
`orthogonal_gate`.  Their real fixes are semantic relocations — moving a literal
into the secret store, or replacing a raw `os.getenv` with the SDK resolution
seam — that change where a credential comes from at runtime.  `recheck-narrowest`
can confirm the *literal / raw read* is gone, but no static gate can confirm the
replacement resolves the **same** secret correctly, and a wrong swap silently
breaks auth in production.  Per design §6.1, a fix no gate can validate must not
be auto-applied.  The safe form is **propose, don't apply**: the model drafts a
concrete diff, a human is the gate.  Some S002 findings are also legitimately
**not** code changes at all — e.g. platform self-auth (`ATLAN_*`) with no SDK
secret-store seam to migrate to — where a justified inline suppression is the
correct disposition, again a human call.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes.

### Execution

```prose
# Suggest-only: detect, draft a fix per finding, route to residue WITHOUT
# applying.  No orthogonal gate can validate an S-series fix, so the human is
# the gate — this area never mutates the working tree.
let violations = call detect-violations
  scope: scope
  series: "S"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  let proposal = call remediate-finding
    finding: finding
    mode: mode

  # The proposal is recorded, never applied.  classification is always
  # "judgment" for S-series, so it lands in the human-review residue.
  add { finding, proposal } to residue with note "S-series suggest-only: proposed fix drafted for human review; NOT applied (no orthogonal gate validates that the replacement resolves the same secret)"
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "security"`._

Drafts a **proposed** fix for human review.  This area is suggest-only: the
proposal is recorded in residue and **never applied** — see **Why suggest-only**
above.  `classification` is always `"judgment"` for both S-rules.  Read the full
function/class context around `finding.line` before drafting.

- **S001 HardcodedCredential** — a string literal is stored as a credential-named
  variable, keyword argument, or dict value.  Draft, in order of preference:

  1. **The real fix (preferred)** — remove the literal and resolve the secret at
     runtime through the SDK seam:
     - inside an `App` subclass with a credential context →
       `cred = await self.context.resolve_credential(ref)` where `ref` is a
       `CredentialRef` (`from application_sdk.credentials import CredentialRef`
       and the `basic_ref`/`api_key_ref` factories);
     - lower-level / ad-hoc →
       `value = await secret_store.get("<name>")` via the `SecretStore` protocol
       (`from application_sdk.infrastructure.secrets import SecretStore`).
     Name the secret key you assumed in the proposal.  Return `outcome = "fix"`.

  2. **Fallback** — if the literal is a genuinely non-secret sample confined to a
     local-only path the discover walk did not exclude (rare), draft an inline
     `# conformance: ignore[S001] <reason>` on the assignment line, where the
     justification explains *why* the value is not a real credential.  Return
     `outcome = "suppress"`.

- **S002 RawEnvCredentialAccess** — a credential-named environment variable is read
  directly via `os.getenv` / `os.environ`.  Draft, in order of preference:

  1. **The real fix (preferred)** — replace the raw read with SDK resolution,
     using the same seams as S001 (`context.resolve_credential(ref)` inside an
     `App`, or `secret_store.get("<name>")` otherwise).  Map the env-var name to
     the credential key and state that mapping in the proposal.  Return
     `outcome = "fix"`.

  2. **Fallback (common for platform self-auth)** — if the value is platform /
     transport auth that the SDK exposes **no** secret-store seam for (e.g. an
     `ATLAN_*` token the app uses to call Atlan itself at process startup), draft
     an inline `# conformance: ignore[S002] <reason>` naming the missing seam.
     Do **not** fabricate a resolution call against a store that cannot supply the
     value.  Return `outcome = "suppress"`.

  Never migrate a dev harness onto the seam: `run_dev*.py` and `scripts/` are
  excluded from detection by design, so an S002 finding there should not occur —
  if one does, treat it as a discover-scope bug, not a remediation target.
