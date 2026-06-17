# Conformance Schema Contract

> **Version:** v1 · **SARIF version:** 2.1.0 · **Profile version:** `atlan/profileVersion = "v1"`
>
> This document is the authoritative reference for the Atlan fleet conformance
> violation schema (BLDX-1385).  It defines what every consumer (CI, Renovate gate,
> OpenProse remediation loop, dashboards) can rely on.

---

## 1. Design rationale

The conformance suite emits **SARIF 2.1.0** as its wire format rather than a bespoke
schema, for three reasons:

1. **Existing art** — SARIF covers ~90% of requirements (rule catalog, findings-with-locations,
   stable fingerprints, fix suggestions, suppressions, invocation exit codes).  GitHub's code
   scanning tab ingests SARIF natively; Trivy already uploads SARIF in this repo.
2. **Industry-standard vocabulary** — consumers, tooling, and reviewers already understand
   `level`, `suppressions`, `kind`, `fixes`.  No bespoke field needs explaining.
3. **Governance gaps filled in `properties` bags** — the few concepts SARIF lacks (`tier`,
   `mechanism`, `orthogonalGate`, `externalInfluence`) go into optional namespaced `properties`
   entries that are additive-safe and never break the schema.

The vendored **`sarif-schema-2.1.0.json`** (from SchemaStore, OASIS provenance) is the
validation contract.  Every produced document must validate against it via
`suite.schema.validate_sarif()`.

---

## 2. Concept → SARIF mapping

| Conformance concept | SARIF construct | Notes |
|---|---|---|
| **Rule catalog** (rules-as-data) | `run.tool.driver.rules[]` (`reportingDescriptor`) | `id`, `name`, `shortDescription`, `helpUri`, `defaultConfiguration.level` |
| **One detected violation** | `run.results[]` | references rule via `ruleId` + `ruleIndex` |
| **File + line** | `result.locations[].physicalLocation` | `artifactLocation.uri` + `region.{startLine,startColumn,…}` |
| **Stable identity** (dedup / oscillation) | `result.partialFingerprints["atlanConformance/v1"]` | SHA-256[:16] of `ruleId + normalised_uri + startLine` |
| **Enforcement tier** warn/block | `result.level` / rule `defaultConfiguration.level` | `block → "error"`, `warn → "warning"`; `atlan/tier` is the source of truth |
| **Inline justified suppression** | `result.suppressions[].kind = "inSource"` | `justification` = the reason; `location` = annotation site |
| **Central allowlist** (future) | `result.suppressions[].kind = "external"` | reserved; mirrors `.security/base-allowlist.json` style |
| **Explicit pass** | `result.kind = "pass"` | lets a clean rule emit a positive result |
| **Proposed fix** | `result.fixes[]` (`artifactChanges`) | populated by the remediation layer (BLDX-1388) |
| **Rule mechanism** | `properties["atlan/mechanism"]` | `"static"` or `"test"` — lets the loop re-run the narrowest gate |
| **Remediation hint** | `result.properties["atlan/hint"]` | machine-actionable hint for the model |
| **Suite version** | `run.tool.driver.version` | = SDK release, e.g. `"3.16.0"` |
| **Repo / commit** | `run.versionControlProvenance[]` | `repositoryUri`, `revisionId` (SHA), `branch` |
| **Gate decision** | `run.invocations[0].exitCode` | `0` = gate passed, `1` = gate failed |
| **Disposition totals** | `run.properties["atlan/summary"]` | `{passing, failing, warning, suppressed}` counts |

---

## 3. The three-state disposition (derived, never stored)

Disposition is **computed from native SARIF fields** by `derive_disposition()` — it is not
stored as a separate field in the report.  This keeps the schema clean (no competing enum)
while giving every consumer a single, unambiguous answer.

| Disposition | `result.kind` | effective `level` | `result.suppressions` | Gate effect | Governance signal |
|---|---|---|---|---|---|
| **PASS** | `"pass"` | any | (n/a) | none | clean-fleet count |
| **FAILING** | `"fail"` | `"error"` | empty | **blocks CI** | violating-app count for `block` rules |
| **WARNING** | `"fail"` | `"warning"` | empty | counted, non-blocking | violating-app count for `warn` rules → graduation signal (BLDX-1393) |
| **SUPPRESSED** | `"fail"` | any | non-empty | non-blocking | **own category** → suppression-rate signal |

**Key invariant:** suppression takes precedence over level.  A `block`-tier result with an
`inSource` suppression is **SUPPRESSED**, not FAILING.  This means inline justified suppressions
are always counted in their own category regardless of the rule's enforcement tier.

**Gate derivation:**

```python
exit_code = 1 if any(derive_disposition(r) == Disposition.FAILING for r in results) else 0
```

Results that are SUPPRESSED or WARNING never fail the gate.

---

## 4. `atlan/*` properties reference

### 4.1 Rule properties (`reportingDescriptor.properties`)

| Key | Type | Required | Description |
|---|---|---|---|
| `atlan/tier` | `"warn"` \| `"block"` | yes | Source of truth for enforcement tier |
| `atlan/mechanism` | `"static"` \| `"test"` | yes | Verification mechanism; controls minimum re-check scope |
| `atlan/category` | `string` | yes | Rule family (e.g. `"silent-swallow"`, `"log-format"`) |
| `atlan/autofixable` | `bool` | yes | Whether the remediation layer can produce a mechanical fix |
| `atlan/orthogonalGate` | `string` | no | Gate a fix PR must not edit (e.g. `"tests"`) |
| `atlan/since` | `string` | no | SDK version the rule was introduced |

### 4.2 Result properties (`result.properties`)

| Key | Type | Required | Description |
|---|---|---|---|
| `atlan/hint` | `string` | no | Machine-actionable remediation hint for the model |
| `atlan/externalInfluence` | `bool` | no | Set by the remediation layer if input included untrusted external content; routes to human review (§6.4 of the design doc) |

### 4.3 Run properties (`run.properties`)

| Key | Type | Required | Description |
|---|---|---|---|
| `atlan/profileVersion` | `string` | yes | Always `"v1"` for this profile |
| `atlan/summary` | `object` | no | `{passing, failing, warning, suppressed}` counts for cheap dashboarding |

---

## 5. Rule catalog (`suite/rules/`)

Rules are **typed Python**, not YAML.  Each series module (`ci.py`, `error_handling.py`,
`logging.py`) exposes a `RULES: tuple[RuleDefinition, ...]`; `suite/rules/__init__.py`
combines them into an immutable `CATALOG: Mapping[str, RuleDefinition]` (O(1) lookup).
Each entry maps to a `reportingDescriptor` and carries the `atlan/*` governance fields.
Rule ID namespaces:

| Prefix | Domain |
|---|---|
| `E001–E099` | Error-handling patterns (from `signal-over-noise` surface phase) |
| `L001–L099` | Logging patterns (from `signal-over-noise` tune phase) |
| `C001–C099` | CI/workflow supply-chain (action-pinning, permissions, trigger hygiene) |
| `T001–T099` | Test-quality patterns (reserved) |
| `D001–D099` | Dependency patterns (reserved) |

**A new rule is just a new entry in the appropriate series module** — it automatically fans
out to every consumer app on the next upgrade, and the suite invalidates every app's
conformance verdict on the next reconcile.  No per-app authoring needed (§4.1 of the design doc).

The per-series rule catalogs below are **auto-generated** from the Python source and live in
`conformance/docs/rules/`.  Regenerate with `uv run poe generate-rule-docs`; CI verifies
they are up-to-date with `uv run poe generate-rule-docs --check`.

| Prefix | Domain | Catalog |
|---|---|---|
| `E001–E099` | Error-handling patterns | [rules/error-handling.md](rules/error-handling.md) |
| `L001–L099` | Logging patterns | [rules/logging.md](rules/logging.md) |
| `C001–C099` | CI/workflow supply-chain | [rules/ci.md](rules/ci.md) |
| `T001–T099` | Test-quality patterns (reserved) | — |
| `D001–D099` | Dependency patterns (reserved) | — |

---

---

## 6. Tiering and suppression governance (BLDX-1393)

### 6.1 Tiers

* **`warn`** — visible, counted, non-blocking.  New rules start here to avoid turning the
  fleet red overnight.
* **`block`** — failures fail the gate and block merges.
* There is intentionally **no `off` tier** — a wrong rule is *deleted*, not parked.

### 6.2 Inline justified suppression

At fleet scale, some rule will be legitimately wrong for one location in one app.  The
mechanism is an **inline annotation in the source file** — it lives next to the code, surfaces
in code review, and is counted in the violation report as its own category.

The suppression is a SARIF `result.suppressions[]` entry:

```json
{
  "kind": "inSource",
  "justification": "optional-dep guard: PIL is never available in CI but is fine",
  "location": {
    "physicalLocation": {
      "artifactLocation": {"uri": "src/connector/extractor.py"},
      "region": {"startLine": 87}
    }
  }
}
```

The detector is responsible for discovering these annotations in the source and emitting
suppressed results (not omitting the result entirely).

**No `off` tier + counted suppressions** = the fleet-wide suppression rate for a rule is
the early-warning signal that the rule is wrong, *before* it graduates to `block`.

### 6.3 Data-triggered graduation (`warn → block`)

Promote a rule from `warn` to `block` when:
- Violating-app count is below threshold **and** not growing (a number the suite already
  emits in `atlan/summary.warning`)
- The canary cohort has been clean for the observation window

A wrong rule promoted prematurely is caught by the fast yank path: a rule-tier override
in the catalog does not require an SDK rollback (§6.3 of the design doc).

---

## 7. Evolution rules

This schema is a **versioned interface** between deterministic detection and probabilistic
remediation.  Evolution rules:

1. **Only add optional fields** — never remove or retype a field in a stable release.
2. **New SARIF-native optional fields** (e.g. `rank`, `security-severity`) are non-breaking —
   add them when decided, no schema change needed.
3. **Profile version** (`atlan/profileVersion`) increments on any breaking change to the
   `atlan/*` vocabulary.
4. **Rule IDs are stable** — never reuse a retired ID.  A deleted rule's ID is retired forever.
5. **Fingerprint key suffix** (`atlanConformance/v1`) increments if the hashing scheme changes,
   so old and new fingerprints coexist without false deduplication.
6. **Rule catalog tiers** (`warn`/`block`) may change between SDK minor versions, but the
   change must be accompanied by a catalog note and staged rollout (canary cohort first).

---

## 8. Consumer guide

### CI / Renovate gate

```sh
# Run the full suite; exit code drives the gate.
uv run atlan-application-sdk-conformance detect --repo . --output report.sarif
echo "Exit code: $?"

# Run only the C001 action-pinning check (low-level):
uv run python -m conformance.suite.checks.actions_pinning --root . .github
```

### Parsing for dashboards

```python
import json
from conformance.suite.schema import derive_disposition, Disposition, SarifReport

report = SarifReport.model_validate(json.loads(open("report.sarif").read()))
for result in report.runs[0].results:
    d = derive_disposition(result)
    # d in {Disposition.PASS, FAILING, WARNING, SUPPRESSED, None}
```

### Reading summary counts (no full parse needed)

```python
summary = report.runs[0].properties.get("atlan/summary", {})
# {"passing": 12, "failing": 0, "warning": 3, "suppressed": 1}
```

### OpenProse remediation loop

The loop consumes `results` where `derive_disposition(r) == Disposition.FAILING` or
`== Disposition.WARNING`.  The `atlan/hint` property provides a machine-actionable starting
point.  The `atlan/mechanism` on the matching rule drives which gate scope to re-run.

---

## 9. Golden example

See `../tests/fixtures/golden_four_dispositions.sarif.json` for a complete, validated
SARIF 2.1.0 document illustrating all four dispositions (PASS, FAILING, WARNING,
SUPPRESSED) against the same source file with two different rules.
