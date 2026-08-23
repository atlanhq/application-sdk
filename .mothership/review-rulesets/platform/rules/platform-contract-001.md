---
schema: 2
id: PLATFORM-CONTRACT-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Activity contracts evolve additively

- MUST NOT remove or rename a field on an activity `Input`/`Output` model —
  in-flight workflows replay old payloads against new code and break.
- New fields are additive with defaults; a new field on an existing
  `Output` states its replay story for payloads that predate it.
- An `Input` that is a manifest/DAG entry point MUST tolerate extra fields,
  or orchestrator-supplied args are silently dropped.
- MUST NOT expose internal implementation knobs as new task inputs —
  default to less control surface; sibling entrypoints share contracts
  (same field, same alias, same default).
