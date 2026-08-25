---
schema: 2
id: APP-VALIDATION-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Preflight and validation use honest oracles

- A preflight/validation check MUST use the same oracle the real operation
  uses — validating by listing what extraction validates by connecting
  produces checks that pass while the run fails (and vice versa).
- Any paginated API read MUST consume all pages (or use a server-side
  filter) before asserting absence — page 1 of a paginated response can be
  legitimately empty while the item exists.
- A check keyed on a response contract MUST be tested against what the
  handler actually emits, not what a form or doc promises.
