---
schema: 2
id: APP-METADATA-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: true
---
# Customer-visible metadata is customer-ready

- Manifest/entrypoint descriptions, display names, and user-facing error
  messages MUST be free of internal jargon, codenames, and implementation
  detail — they render in customer tenants and demos.
- Naming follows the fleet convention (no vendor-prefix inconsistency:
  either the product name style used by sibling connectors or a stated
  reason to differ).
- User-facing errors state what the user can do, not internals.
