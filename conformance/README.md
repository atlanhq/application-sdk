# Atlan Fleet Conformance

Deterministic rule-checking infrastructure for the Fleet Drift Remediator
([BLDX-1384](https://linear.app/atlan-epd/issue/BLDX-1384) /
[BLDX-1385](https://linear.app/atlan-epd/issue/BLDX-1385)).

## What lives here

| Path | Purpose |
|---|---|
| `src/conformance/schema/` | Typed Pydantic models for the SARIF 2.1.0 profile, `Disposition` engine, rule-catalog loader, `ReportBuilder`, and schema validator |
| `src/conformance/schema/sarif-schema-2.1.0.json` | Vendored official SARIF schema — the validation contract for all emitted reports |
| `src/conformance/rules/catalog.yaml` | Rules-as-data: every rule the suite knows about, expressed as SARIF `reportingDescriptor` records with `atlan/*` governance fields |
| `docs/schema-contract.md` | Concept-to-SARIF mapping, the three-state disposition table, `properties` extension reference, evolution rules, and a golden four-disposition example |
| `tests/` | Schema round-trip, disposition coverage, and golden-example validation tests |

## Not for production runtime

This package is **conformance testing infrastructure**, not a production runtime
dependency.  It ships with SDK releases so consumer apps automatically receive the
latest rule set on upgrade, but it is never imported by `application_sdk`.

## Quick start

```python
from conformance.schema import ReportBuilder, Disposition, derive_disposition

builder = ReportBuilder(tool_name="atlan-conformance", tool_version="3.16.0")
builder.add_result("LOG001", "src/foo.py", 42)
report = builder.build()

# Derive gate decision
from conformance.schema import derive_disposition, SarifReport
dispositions = [derive_disposition(r) for r in report.runs[0].results]
failing = [d for d in dispositions if d == Disposition.FAILING]
exit_code = 1 if failing else 0
```

See `docs/schema-contract.md` for full field reference.
