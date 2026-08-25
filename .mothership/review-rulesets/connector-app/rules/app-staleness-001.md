---
schema: 2
id: APP-STALENESS-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: true
---
# Code-reality drift

- FLAG comments and docstrings in or around the diff that describe behavior
  the code no longer has — stale documentation misleads the next fix.
- FLAG hardcoded vendor-shaped lists the diff touches (exclusion lists,
  system-object names, type maps) that have plausibly decayed against the
  source's current naming — ask for the generation source or a refresh
  story.
- A config or credential field that is parsed/normalized but never consumed
  at every construction site is drift, not dead code — it resurfaces as a
  misdiagnosed runtime failure. Audit all construction sites when the diff
  touches one.
