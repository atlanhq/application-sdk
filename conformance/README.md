# Atlan Fleet Conformance

Deterministic rule-checking infrastructure for the Fleet Drift Remediator
([BLDX-1384](https://linear.app/atlan-epd/issue/BLDX-1384) /
[BLDX-1385](https://linear.app/atlan-epd/issue/BLDX-1385)).

## What lives here

| Path | Purpose |
|---|---|
| `suite/schema/` | Typed Pydantic models for the SARIF 2.1.0 profile, `Disposition` engine, `RuleDefinition` model, `Finding` contract, `ReportBuilder`, and schema validator |
| `suite/schema/sarif-schema-2.1.0.json` | Vendored official SARIF schema — the validation contract for all emitted reports |
| `suite/rules/` | Typed Python rule definitions, one module per ID series (`ci.py`, `error_recovery.py`, `logging.py`); auto-combined into an immutable dict-keyed `CATALOG` at import time |
| `suite/checks/` | Deterministic check modules — one per rule family; each exposes `scan_text`, `scan_path`, `discover`, and a `main(argv)` CLI |
| `suite/runner.py` | Suite dispatcher: runs all registered checks, merges findings into one SARIF report |
| `docs/schema-contract.md` | Concept-to-SARIF mapping, the three-state disposition table, `properties` extension reference, evolution rules, and a golden four-disposition example |
| `tests/` | Schema round-trip, disposition coverage, golden-example validation, and per-check tests |

## Not for production runtime

This directory is **conformance testing infrastructure**, not a production runtime
dependency, and is **not bundled into the published PyPI package** (`packages =
["application_sdk"]` in `pyproject.toml`).  Consumer apps receive the latest rule
set by sourcing this directory directly from the SDK repository — the reusable CI
workflow sparse-checkouts `conformance/` at a given `sdk-ref` rather than
installing the wheel.

## Quick start

```python
from suite.schema import ReportBuilder, Disposition, derive_disposition

builder = ReportBuilder(tool_name="atlan-conformance", tool_version="3.16.0")
builder.add_result("P001", "src/foo.py", 42)
report = builder.build()

# Derive gate decision
dispositions = [derive_disposition(r) for r in report.runs[0].results]
failing = [d for d in dispositions if d == Disposition.FAILING]
exit_code = 1 if failing else 0
```

## Running checks

```sh
# Full suite (all registered checks → one merged SARIF):
PYTHONPATH=conformance python -m suite.runner --repo . --output report.sarif

# C001 only (action-pinning check):
PYTHONPATH=conformance python -m suite.checks.actions_pinning --root . .github
```

See `docs/schema-contract.md` for full field reference.
