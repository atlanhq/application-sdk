---
schema: 2
id: APP-ERRORS-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Typed errors at boundaries; retry and failure are explicit

- MUST raise typed application errors at module boundaries — never bare
  `ValueError`/`RuntimeError` for operational failures.
- MUST preserve the cause chain (`raise NewError(...) from exc`) AND keep it
  acyclic: never re-raise where the new error already appears in the cause
  chain — a cycle wedges the platform's failure serialization and replaces
  the real error with a recursion error.
- MUST NOT write `except Exception: return Output(FAILED, str(exc))` at a
  handler or endpoint — it erases the failure class the platform routes on.
- Retryable errors re-raise so the platform retries; non-retryable errors
  return a failure shape. A new catch decides which, explicitly.
- Unclassified source errors default to retryable, and fail-fast branches
  key on stable error codes, never message prose — a wrong non-retryable
  verdict kills a multi-hour run on attempt 1.
