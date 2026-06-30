---
kind: responsibility
name: contract-toolkit-area
description: >
  Maintains the current K-series violation-set and drives remediation of
  legacy-contract findings.  K001 (contract amends NativeApp.pkl or
  NativeAppBundle.pkl instead of App.pkl) and K002 (NativeApp-only APIs present:
  flatManifestArgs, manifestMetadataArgs, workflowTypeOverride, or legacy
  Config/Connectors/Credential/Renderers imports) are guided migration fixes
  verified by the pkl-eval gate.
---

### Maintains

The current set of unsuppressed K-series (contract-toolkit conformance)
findings in the working tree, classified by disposition and remediability.

#### violations-contract-toolkit

The fingerprint-set of all unsuppressed FAILING/WARNING K-series results in the
current working tree, as reported by `suite.runner --series K`.

K-series rules are WARN-tier, so in **default** mode this facet is always empty
(warnings do not fail the gate).  In **strict** mode the fingerprint-set includes
unsuppressed WARNING results, which is where K-series remediation actually runs.

The active scope decides which rules can appear: both K001 and K002 are
`scope=APP`, so they surface only on consumer app repos.  The runner
auto-detects scope, so the SDK repo sees 0 findings.

This facet's fingerprint moves when any K-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series K` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for K-series in the SARIF output is 0 (every
> K-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `contract/**/*.pkl` file under `scope`
changes.  In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "K"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "contract-toolkit"`._

Both K001 and K002 are **WARN-tier** — they surface only under `--strict` mode.
Before proposing any edit, read the actual lines around `finding.line` in
`finding.file` (a `contract/*.pkl` file).  **Never edit `atlan.yaml`,
`app/generated/`, or any other generated artifact directly** — those are outputs
of `pkl eval`; C002 catches staleness.  After every edit, the `pkl-eval-gate`
runs `pkl eval` to verify the migration compiled and regenerated cleanly.

---

**K001 ContractAmendsLegacyModule** — the contract file's `amends` line points at
a legacy module (`NativeApp.pkl` or `NativeAppBundle.pkl`) instead of `App.pkl`.
`classification = "judgment"` (structural migration; no two contracts are
identical).  After applying the edit, the `pkl-eval` gate determines whether the
migration compiled.

*Procedure:*

1. **Change the amends line** to `amends "@app-contract-toolkit/App.pkl"` (for a
   package-dependency contract) or `amends "../../src/App.pkl"` (for the
   toolkit's own examples/tests).

2. **Fix the workflowType** — NativeApp.pkl used a `workflowType`
   (PascalCase) + optional `workflowTypeOverride` pair with automatic
   kebab-casing.  App.pkl accepts the verbatim string.  Take the string that the
   legacy contract would have emitted (apply PascalCase→kebab-case manually if
   needed), and set it as App.pkl's `workflowType`.  Omit `workflowType`
   entirely when it equals the `name` field (App.pkl defaults to kebab-casing
   `name`).  Drop `workflowTypeOverride` — it does not exist in App.pkl.

3. **Fix the connector field** — `NativeApp.pkl` required `connector` (it typed
   it as `Connectors.Type`).  `App.pkl` makes `connector` nullable; utility apps
   that have no connector type should set `connector = null` or omit the field.

4. **Remove NativeAppBundle wiring** (K001 on `NativeAppBundle.pkl`) — the
   bundle entrypoints should be collapsed into App.pkl's typed `entrypoints`
   block.  Each per-entrypoint contract file (`amends "@app-contract-toolkit/
   NativeApp.pkl"`) must also be migrated (K001 will fire for each file separately).

5. **Stage the `contract/*.pkl` edits** — do NOT run `pkl eval` manually; the
   `pkl-eval` gate does it and captures the result.

If `NativeAppBundle.pkl` is involved and the migration requires restructuring
multiple entrypoint files, `classification = "judgment"` and every migrated file
is individually residue-listed for human review.

---

**K002 LegacyContractApi** — the contract file carries NativeApp-only knobs or
imports that do not exist in `App.pkl`.  `classification = "judgment"` (each
knob requires understanding the intent).

Read the specific knob(s) named in the `finding.message` and apply:

- **`flatManifestArgs`** and **`manifestMetadataArgs`** — remove both properties.
  `App.pkl` always emits flat top-level args; these flags are structural no-ops.

- **`workflowTypeOverride`** — see K001 step 2 above.  Remove the property after
  resolving it into the `workflowType` field.

- **`import "…Config.pkl"`** — remove the import.  Switch any `Config.*` widget
  references to the re-exported `Widgets.*` typealiases provided by `App.pkl`
  directly (e.g. `Config.UIConfig` → `UIConfig`, `Config.TextInput` →
  `TextInput`).

- **`import "…Connectors.pkl"`** — remove the import.  App.pkl re-exports all
  connector constants as `Connectors.*` with no import needed.

- **`import "…Credential.pkl"`** (Argo-era) and **`import "…Renderers.pkl"`**
  (Argo-era) — remove both; these modules are no longer used by App.pkl.

If the contract file also has a K001 finding (amends a legacy module), address
K001 first — many K002 knobs disappear automatically when the module changes
because App.pkl lacks those properties.

---

**Suppress outcome (strict mode only, WARNING-tier findings)**: the model may
propose an inline `// conformance: ignore[Kxxx] <8–40 word justification>` on
the violating line or the comment-only line directly above it when a legitimate
exception exists (e.g. a K001 finding on a contract intentionally kept at the
legacy module during a phased migration with a tracked follow-on ticket).  Route
every suppression to residue for human audit.
