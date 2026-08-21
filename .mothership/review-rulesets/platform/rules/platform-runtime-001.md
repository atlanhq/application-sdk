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

- MUST NOT call `datetime.now()`, `uuid4()`, `random`, or read env/config
  dynamically inside a workflow `run()` — replay produces different values
  and the workflow non-deterministically diverges.
- MUST NOT await object-store, state-store, or HTTP I/O from the workflow
  body — the sandbox lets it through silently and the workflow hangs. All
  I/O belongs in activities.
- MUST NOT `import temporalio` in app code — the SDK owns the Temporal seam;
  use typed inputs / `workflow_args`.
- Run-scoped values (output paths, markers) come from activity outputs, not
  from helpers evaluated in the workflow body.
