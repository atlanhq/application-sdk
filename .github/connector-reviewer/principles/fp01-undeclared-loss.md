---
id: FP01
applies-to: [app]
---
# Never publish undeclared loss

Principle: A run that reports success has published everything in scope. When a slice is missing,
skipped or degraded, the run fails or says so: `PARTIAL_SUCCESS`, a `skip_record` entry, or a gap
named in its output.
Why: Publishing treats what is absent from the output as deleted, so a quiet shortfall removes real
assets from the customer's catalog while the run stays green.
Ask:
- Can any path in this diff (a caught error, a filter, a page loop that ends early, a missing input read as empty) drop records while the run still reports success?
- Is completeness between steps checked against what the producer declared it wrote, per source, and not by listing a storage prefix or adding counts across sources?
- If the change narrows coverage on purpose, does the PR say so and change the declaration or count logic with it?
Don't flag: shapes conformance reports: an HTTP failure returned as an empty result (`E020`), an
`except` that returns a value or leaves a loop quietly (`E007`, `E014`), swallowed exceptions
(`E001`, `E002`), unexamined `gather` results (`E010`). Do not flag a declared degradation or a
record skipped through `skip_record`.
Severity: High when assets can go missing or be deleted under a success status. Medium when the gap
is declared but the declaration is incomplete or hard to find.
