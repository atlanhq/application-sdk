---
schema: 2
id: APP-ERRORS-001
level: L2
category: correctness
globs: []
severity: HIGH
provenance: sdk-v3-connector-review-guidelines; SDK error taxonomy
suppressible: false
---
# Typed errors at boundaries; retry and failure are explicit

- MUST raise typed application errors at module boundaries — never bare
  `ValueError`/`RuntimeError` for operational failures.
- MUST preserve the cause chain: `raise NewError(...) from exc`.
- MUST NOT write `except Exception: return Output(FAILED, str(exc))` at a
  handler or endpoint — it erases the failure class Temporal and the
  Automation Engine route on.
- Retryable errors are re-raised so the platform retries; non-retryable
  errors return a failure shape. A new catch must decide which, explicitly.
- Default for an unclassified source error is retryable — a wrong
  non-retryable verdict kills a multi-hour run on attempt 1.
