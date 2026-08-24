---
kind: function
name: detect-violations
description: >
  Runs the conformance suite for one or more rule-series, writes the full SARIF
  report to disk, and returns a list of actionable findings tagged with their
  area.  Deterministic — the suite runner is the source of truth, not the model.
---

### Parameters

- `scope` (string, required) — path to the **repository root** to scan.  Always
  the repo root (the directory passed to `--repo`), never a subdirectory.  Use
  `path_prefix` to scope results to a subtree.
- `path_prefix` (string, optional) — repo-root-relative directory prefix to
  restrict results to, e.g. `"application_sdk"`.  Applied as a **post-filter**
  on result URIs after the runner produces the full-repo report.  The runner has
  no `--include` flag; filtering is always done on the parsed output.  When
  omitted, all results are returned.
- `series` (string, default `"E,L,C,P,O,D,B,I,T,K,S"`) — comma-separated list of
  rule-series letters to run, e.g. `"E"` for error-handling only or `"E,L"` for
  error-handling and logging.
- `rule_ids` (list of string, optional) — restrict results to these exact rule
  IDs, e.g. `["L004"]` or `["L001", "L011"]`.  **Pushed down to the runner
  natively**: pass them as `--rule <ID>[,<ID>...]` (comma-joined, in place of
  `--series`) — the runner derives the series from the ids, executes only those
  series' modules, and scopes the findings, the exit state, and the emitted
  SARIF rule catalog to exactly the requested rules.  Do NOT pass `--series`
  alongside `--rule`; the two are mutually exclusive and the runner errors on
  the combination.

  **Version fallback**: `--rule` exists from suite 0.21.  If the pinned
  runner rejects the flag (exit with an unknown-argument error), rerun with
  the narrowest `--series` covering the ids (the first letter of each ID) and
  apply the rule-id post-filter on the parsed output as described below — the
  behaviour is identical, only the efficiency differs.  Never use
  `--series L004`: a series flag matches a single *letter*, so a full rule id
  silently activates **zero** checks and produces an empty report.

  Tier is a **per-rule** property, not a per-series one (D001 and D009 are
  BLOCK inside a series that is otherwise WARN), so a caller that needs "only
  this rule" — or "only the blocking rules" — has no way to express it through
  `series` alone.  This parameter is that way.  When omitted, all rules in the
  requested series are returned.
- `target` (string, default `"failing"`) — which dispositions to return.
  `"failing"` returns only FAILING results (BLOCK-tier, gate-blocking).
  `"failing+warning"` (used by strict mode) also includes WARNING results
  (WARN-tier, non-blocking but in-scope for remediation).

### Returns

- `sarif_path` — path to the written SARIF file (`remediation/runs/detect.sarif`).
- `findings` — list of findings, each with:
  - `rule_id` — e.g. `"E002"`.
  - `area` — series letter mapped to area name: `E` → `error-handling`,
    `L` → `logging`, `C` → `ci`, `P` → `prescriptions`, `O` → `optimizations`,
    `D` → `dependency`, `B` → `deprecation`, `I` → `dockerfile`, `T` → `tests`,
    `K` → `contract-toolkit`, `S` → `security`.
  - `file` — repo-relative path.
  - `line`, `column` — location.
  - `fingerprint` — value of `partial_fingerprints["atlanConformance/v1"]`;
    stable across runs on the same source line; used for oscillation detection.
  - `disposition` — `"failing"` or `"warning"`.
  - `mechanism` — `"static"` or `"test"` (from `atlan/mechanism`).
  - `autofixable` — boolean (from `atlan/autofixable`).
  - `orthogonal_gate` — string or null (from `atlan/orthogonalGate`).
  - `forces_external_influence` — boolean (from `atlan/forcesExternalInfluence`,
    default `false`). Structural, rule-level flag — `true` for a rule whose
    fix always consults untrusted external content (currently only C001),
    independent of whatever `remediate-finding`'s own per-invocation
    `external_influence` result reports. `detect-fix-recheck` ORs the two
    together so residue-routing for such a rule doesn't depend on the model
    remembering to set its own flag on every single call.
  - `hint` — string or null (from `atlan/hint`).
  - `message` — human-readable violation message from the runner.

### Implementation

Run the conformance suite runner, write the SARIF to
`remediation/runs/detect.sarif`, then parse it.  With `rule_ids`, scope the
runner natively:

```sh
mkdir -p remediation/runs
uv run atlan-application-sdk-conformance detect \
  --repo <scope> \
  --rule <rule_ids comma-joined> \
  --output remediation/runs/detect.sarif
```

Without `rule_ids` (whole-series runs), use `--series` as before:

```sh
uv run atlan-application-sdk-conformance detect \
  --repo <scope> \
  --series <series> \
  --output remediation/runs/detect.sarif
```

```python
import json
with open("remediation/runs/detect.sarif") as f:
    raw = json.load(f)
report = conformance.suite.schema.sarif.SarifReport.model_validate(raw)
```

**Field names use Pydantic snake_case**, not the raw SARIF JSON aliases.  The
complete mapping (JSON alias → Pydantic attribute):

| Purpose | Access path |
|---|---|
| Rule ID | `result.rule_id` |
| Rule index (into driver.rules) | `result.rule_index` |
| Fingerprint | `result.partial_fingerprints.get("atlanConformance/v1", "")` |
| Message text | `result.message.get("text", "")` |
| File URI | `result.locations[0].physical_location.artifact_location.uri` |
| Start line | `result.locations[0].physical_location.region.start_line` |
| Start column | `result.locations[0].physical_location.region.start_column` |
| Rule properties | `run.tool.driver.rules[result.rule_index].properties` |
| Result hint | `result.properties.get("atlan/hint", None)` |

For each result in `run.results`, call `derive_disposition(result)` from
`conformance.suite.schema.disposition`.  Keep only results whose disposition matches the
`target` parameter (FAILING always included; WARNING included only when target
is `"failing+warning"`).

**Path prefix filtering**: if `path_prefix` is set, discard any result whose
`result.locations[0].physical_location.artifact_location.uri` does not start
with the prefix string (normalise both sides with `str.lstrip("./")` before
comparing so that `"./application_sdk/foo.py"` matches `"application_sdk"`).

**Rule ID filtering**: when the runner ran with `--rule`, the SARIF is already
rule-scoped and no further filtering is needed.  On the version-fallback path
(older pinned runner, `--series` + post-filter), discard any result whose
`result.rule_id` is not in `rule_ids`.  Compare exactly — rule IDs are already
canonical (`^[A-Z]\d{3}$`); upper-case any caller-supplied value once before
comparing so a `--rule l004` argument still matches.

Either way the filter composes with `path_prefix` — a caller can ask for
"L004, only under `app/`".  An empty or absent `rule_ids` means "no rule
filter", never "match nothing"; an ID that matches no result returns an empty
`findings` list, which is the correct answer (that rule is clean) and must not
be reported as an error.

Tag each result's area by reading the first letter of `result.rule_id`:
`E` → `error-handling`, `L` → `logging`, `C` → `ci`, `P` → `prescriptions`,
`O` → `optimizations`, `D` → `dependency`, `B` → `deprecation`,
`I` → `dockerfile`, `T` → `tests`, `K` → `contract-toolkit`, `S` → `security`.

Extract `atlan/mechanism`, `atlan/autofixable`, `atlan/orthogonalGate`,
`atlan/forcesExternalInfluence` (default `false` if absent) from
`run.tool.driver.rules[result.rule_index].properties`, and `atlan/hint` from
`result.properties`.  Return `sarif_path` and the structured `findings` list.

Return `sarif_path` and an empty `findings` list (not an error) if the runner
exits 0 or if no results match the requested dispositions.
