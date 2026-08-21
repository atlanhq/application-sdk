---
schema: 2
id: PLATFORM-CONTRACT-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
provenance: sdk-v3-platform-review-guidelines; Temporal replay semantics
suppressible: false
---
# Activity contracts evolve additively

- MUST NOT remove or rename a field on an activity `Input`/`Output` model —
  in-flight workflows replay old payloads against the new code and break.
- New fields are additive with defaults; a new field on an existing `Output`
  needs a stated replay story for payloads that predate it.
- An `Input` that is a manifest/DAG entry point MUST tolerate extra fields,
  or orchestrator-supplied args are silently dropped.
