---
schema: 2
id: PLATFORM-CONTRACT-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Contract evolution needs a replay story

The conformance suite BLOCKS non-additive contract changes and contract
drift mechanically. This rule owns what "additive" cannot capture:

- A new field on an existing activity `Output` states its replay story:
  what happens when an in-flight workflow replays a payload that predates
  the field? A default alone is only correct if downstream logic tolerates
  it mid-run.
- Entry-point `Input`s DECLARE every manifest-supplied field the app
  consumes. The SDK warns and DROPS undeclared extras — an orchestrator
  arg the app expects but never declared is silently lost, not tolerated.
  FLAG code that reads platform args from raw dicts to dodge declaration,
  and any `extra="allow"` added just to smuggle undeclared fields through.
- MUST NOT expose internal implementation knobs as new task inputs —
  default to less control surface; sibling entrypoints share contracts
  (same field, same alias, same default).
