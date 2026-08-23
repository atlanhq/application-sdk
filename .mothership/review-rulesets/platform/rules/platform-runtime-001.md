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

- Every `await` in a workflow `run()`/entrypoint body MUST resolve to a
  platform primitive (activity, timer, signal). A plain async helper that
  touches the object store, HTTP, or the filesystem suspends on a
  non-replayable future — the workflow goes dormant until run-timeout
  (observed as multi-day hangs). The sandbox does not always raise.
- MUST NOT call `datetime.now()`, `uuid4()`, `random`, `time.time()`, or
  read env/config dynamically in workflow scope — use the SDK seams
  (`self.now()`, `self.uuid()`); replay diverges otherwise.
- MUST NOT `import temporalio` in app code — the SDK owns the platform
  seam; use typed inputs / `workflow_args`.
- Run-scoped values (output paths, markers) come from activity outputs,
  never from helpers evaluated in the workflow body.
- Conformance flags these WARN-only (non-blocking); this review owns the
  judgment. Guarded `workflow.unsafe.imports_passed_through()` blocks with
  a written justification are the sanctioned exception.
