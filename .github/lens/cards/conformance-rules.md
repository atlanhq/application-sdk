# conformance-rules: Conformance suite and remediation
- Flag: a rule added/changed under `suite/rules/` or `suite/checks/` without its paired `remediation/**` change (check `read_diff`), or the reverse.
- Flag: a new rule with no behaviour test (`tests/test_*_conformance.py`) covering a positive and a negative case, or not asserted in `tests/test_catalog.py` (ID, series, scope).
- Flag: wrong series (E error, L logging, C CI, D dependency, P prescriptions, O optimisations, I dockerfile; S/B/T/A reserved), duplicate IDs, wrong scope (`sdk`/`app`/`both`).
- Flag: a new BLOCK-tier rule that would fail the dogfooded run on `application_sdk/**` without fixing violations or staging at WARN.
- Flag: false positives/negatives on forms real SDK or connector code uses. Judge the strategy first; fixes are "pin with a test + docstring note", never data-flow or name-binding resolution.
- Flag: rules needing the resolved env (D series) placed where uvx-isolated legs run them as no-ops.
- Flag: SARIF messages/evidence holding secret values; fixtures with real secrets or customer names.
- Flag: `autofixable` contradicting whether the fix is mechanical; remediation gates that can pass vacuously.
- Severity: critical if a secret reaches SARIF; high if the rule misfires on common code, breaks the dogfood gate, or ships unpaired; medium for missing tests/catalog asserts; low otherwise.
