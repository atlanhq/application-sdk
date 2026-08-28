---
schema: 2
id: APP-PREFLIGHT-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# New source access needs preflight permission coverage

A config or permission problem must be surfaced at preflight — where the
gate reports it, and blocks for hard-mode apps — not discovered hours into
extraction. Every new way the app reaches the source widens the grant it
silently assumes.

- TRIGGER: the diff adds or widens source access — a new API endpoint or
  scope, a new SQL statement, system table/view or schema, a new client
  method, a new credential/connection field the source authorizes against.
- The app's `preflight_check` MUST verify the credential can perform that
  access. Reaching the source only from an extraction activity, with no
  preflight counterpart, is the finding.
- "Already covered" is a valid answer, but MUST be stated: name the
  existing check and why it exercises the same grant. An unnamed claim of
  coverage IS the finding — same object, different privilege (SELECT vs
  SHOW, read vs list, table vs view) is not coverage.
- Ground the grant, do not guess it. Check official source documentation
  for the exact endpoint, scope, SQL object, or client operation, and cite
  the documented permission and its URL. Never infer a permission from a
  method or object name. If documentation is unavailable or ambiguous, do
  not raise a finding on the guess: leave this rule `checked`, raise no
  finding, and put the open question in the review summary — name the
  operation and ask the author for an authoritative citation.
- Weight the silent case hardest: a missing grant that returns an empty
  result instead of erroring produces a green run with missing assets.
  A loud mid-run auth failure is the lesser sibling — neither is
  acceptable, but the empty one decides severity.
- The check itself is still bound by APP-VALIDATION-001 — same oracle as
  the real operation. A check that lists what extraction queries passes
  while the run fails.
- Do not review for a handler that blocks. The handler returns an honest
  `READY`, `PARTIAL`, or `NOT_READY` verdict; `App.preflight_gate_mode`
  alone decides whether `NOT_READY` blocks. Requesting a raise or a block
  inside the handler is the wrong fix; the missing check is the finding.
- Safe path: the check exists in the same PR, or the PR names the covering
  check, or it states why the access needs no grant beyond one already
  checked.
