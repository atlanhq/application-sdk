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
- `series` (string, default `"E,L,C,P,O"`) — comma-separated list of rule-series
  letters to run, e.g. `"E"` for error-handling only or `"E,L"` for
  error-handling and logging.
- `target` (string, default `"failing"`) — which dispositions to return.
  `"failing"` returns only FAILING results (BLOCK-tier, gate-blocking).
  `"failing+warning"` (used by strict mode) also includes WARNING results
  (WARN-tier, non-blocking but in-scope for remediation).

### Returns

- `sarif_path` — path to the written SARIF file (`remediation/runs/detect.sarif`).
- `findings` — list of findings, each with:
  - `rule_id` — e.g. `"E002"`.
  - `area` — series letter mapped to area name: `E` → `error-handling`,
    `L` → `logging`, `C` → `ci`, `P` → `prescriptions`, `O` → `optimizations`.
  - `file` — repo-relative path.
  - `line`, `column` — location.
  - `fingerprint` — value of `partial_fingerprints["atlanConformance/v1"]`;
    stable across runs on the same source line; used for oscillation detection.
  - `disposition` — `"failing"` or `"warning"`.
  - `mechanism` — `"static"` or `"test"` (from `atlan/mechanism`).
  - `autofixable` — boolean (from `atlan/autofixable`).
  - `orthogonal_gate` — string or null (from `atlan/orthogonalGate`).
  - `hint` — string or null (from `atlan/hint`).
  - `message` — human-readable violation message from the runner.

### Implementation

Run the conformance suite runner for the requested series, write the SARIF to
`remediation/runs/detect.sarif`, then parse it:

```sh
mkdir -p remediation/runs
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

Tag each result's area by reading the first letter of `result.rule_id`:
`E` → `error-handling`, `L` → `logging`, `C` → `ci`, `P` → `prescriptions`,
`O` → `optimizations`.

Extract `atlan/mechanism`, `atlan/autofixable`, `atlan/orthogonalGate` from
`run.tool.driver.rules[result.rule_index].properties`, and `atlan/hint` from
`result.properties`.  Return `sarif_path` and the structured `findings` list.

Return `sarif_path` and an empty `findings` list (not an error) if the runner
exits 0 or if no results match the requested dispositions.
