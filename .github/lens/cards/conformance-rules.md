# conformance-rules: Conformance suite (packages/conformance)
- Layout: rules `packages/conformance/conformance/suite/rules/`, checks `packages/conformance/conformance/suite/checks/`, guidance `packages/conformance/conformance/programs/areas/<area>.prose.md`. No top-level `remediation/`; never ask for one.
- Flag: a new BLOCK rule or WARN→BLOCK flip with no count of current fleet violations (a BLOCK reds the fleet at publish, `packages/conformance/conformance/docs/schema-contract.md`).
- Flag: a broadened BLOCK detector with no census; a narrowed one with no test pinning the dropped form; misfires on forms real code uses.
- Flag: a reused/renumbered rule ID; an interim rule with no `superseded_by`/`until`.
- Flag: a new rule without positive, negative and suppression cases in `packages/conformance/tests/` or its series set in `test_catalog.py`; an autofixable app/both or D rule with no `**<ID> Name**` area-prose bullet.
- Flag: a `canonical_reference` the detector never reads; vague BLOCK "Customer impact:"; wrong scope; `exclude-paths-*` widened in `.github/workflows/sdk-gate.yaml` to get green.
- Flag: rule docs not regenerated (`gen-rule-docs --check`, not in CI); secrets in SARIF; real secrets or customer names in fixtures.
- Don't flag (tests enforce): duplicate/malformed IDs, missing rationale/scope, non-reference-app `canonical_reference`, autofixable-vs-prose contradictions.
- Severity: critical if a secret reaches SARIF; high if a rule misfires, reds the fleet or breaks the SDK gate; medium otherwise.
