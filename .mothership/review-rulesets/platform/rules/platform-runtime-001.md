---
schema: 2
id: PLATFORM-RUNTIME-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Workflow bodies are deterministic orchestration only

The conformance suite flags DIRECT violations mechanically:
non-deterministic primitives, direct I/O, and raw platform imports in
workflow scope. This rule owns the indirection static analysis cannot
trace:

- Every `await` in a workflow `run()`/entrypoint body MUST resolve to a
  platform primitive (activity, timer, signal). A plain async helper that
  touches the object store, HTTP, or the filesystem — however deep in its
  call chain — suspends on a non-replayable future and the workflow goes
  dormant until run-timeout (observed as multi-day hangs). The sandbox
  does not always raise, and the suite cannot follow the chain.
- Run-scoped values (output paths, markers) come from activity outputs,
  never from helpers evaluated in the workflow body — the body's helper
  resolves against replay-time state.
- Guarded `workflow.unsafe.imports_passed_through()` blocks with a written
  justification are the sanctioned exception; an unexplained new one is a
  finding.
