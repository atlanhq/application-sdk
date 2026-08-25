---
schema: 2
id: APP-ERRORS-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Retry and failure semantics are explicit

The conformance suite owns exception hygiene mechanically (bare excepts,
untyped raises, missing chaining, swallowed errors). This rule owns the
semantics no AST check can judge:

- Cause chains stay acyclic: never re-raise where the new error already
  appears in its own cause chain — a cycle wedges the platform's failure
  serialization and replaces the real error with a recursion error.
- Retryability (does the platform retry) and degradability (may an
  exhausted failure be absorbed as a gap and still report success) are
  SEPARATE decisions. A new classifier branch answers both, explicitly;
  never reuse one boolean for both.
- Unclassified source errors default to retryable — a wrong non-retryable
  verdict kills a multi-hour run on attempt 1.
- Fail-fast classification keys on stable error codes/enums, never on
  message prose — sources keep rewording their errors.
- A catch that converts a failure into a success-shaped return erases the
  failure class the platform routes on; verify the caller can still
  distinguish outcomes.
