---
id: FP08
applies-to: [contract, generated, app]
---
# Contracts only grow

Principle: A released `Input` or `Output` field is never removed, renamed, retyped or made
required. A change is a new field with a default that old callers and running workflows can live
with, declared in the contract and regenerated, not read from somewhere the contract cannot see.
Why: Workflows started on the old code replay on the new code, and other apps and the platform
still send and read the old shape. A broken contract corrupts runs in flight or drops a value with
no error.
Ask:
- For each new field: what happens when a run started before this change replays without it, and is the default safe for the code that reads it mid-run?
- Does code take a platform or manifest value from a raw dict or an `extra="allow"` model instead of declaring it on the contract?
- Does a new input expose an internal tuning knob, or differ in name, alias or default from the same field on a sibling entrypoint?
Don't flag: what conformance blocks or reports: removed, renamed, retyped or newly required fields
(`B005`), a stale ledger (`B006`), untyped boundaries (`P013`, `P014`), manifest and form keys that
miss the contract (`K006`, `K018`, `K019`, `K021`), stale or hand-edited generated files (`K003`,
`K004`, `K005`), `getattr` with a default on a typed field (`P026`). Do not review generated code
line by line: it follows the contract.
Severity: High when runs in flight or another app would break or silently lose a value. Medium for
a new knob or an inconsistent default that is safe today.
