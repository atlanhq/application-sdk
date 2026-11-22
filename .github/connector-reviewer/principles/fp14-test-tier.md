---
id: FP14
applies-to: [app, tests]
---
# A change comes with a test in the right tier

Principle: A change in behaviour comes with a test that fails if the change is undone, in the tier
that can see it. Unit tests cover logic. Integration tests cover anything about the source: auth
modes, filters, schema shapes, client and engine changes, new flags. End-to-end tests cover only
orchestration, publish layout and the setup flow.
Why: A test in the wrong tier passes while production breaks. A unit test that mocks the client or
the config reader cannot see a wrong query, a paging bug or a flag that never reaches the code.
Ask:
- Which test in this PR fails if the change is reverted? If none, name the function or path left unproven.
- Can that test's tier see the change? Logic needs a unit test; anything decided by the source, an engine, storage or config needs an integration test through the real path, with a new flag run on and off.
- Do the test inputs look like what the source sends, including the awkward ones (empty, one item, a page boundary, an empty page that still has a next token)?
Don't flag: what conformance reports: missing tiers (`T010`, `T011`, `T012`), tests outside the
tier folders or deselected (`T013`, `T001`, `T018`), end-to-end suites CI never runs or entrypoints
without one (`T021`, `T025`), coverage floors (`T014`, `T015`), preflight scenarios (`F016`). Do
not ask for an end-to-end test unless orchestration, publish layout or the setup flow changed. Do
not flag refactors that existing tests pin, docs, or generated code.
Severity: High when a change to extraction, identity or published output has no test that would
catch its regression. Medium when a test exists but sits in a tier that cannot see the change.
