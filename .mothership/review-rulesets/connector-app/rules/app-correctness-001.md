---
schema: 2
id: APP-CORRECTNESS-001
level: L2
category: correctness
globs: []
severity: HIGH
provenance: application-sdk
suppressible: false
---
# Return complete source results

When changed connector code reads a paginated, streamed, or batched source API,
verify that it processes every page and propagates failures. Report a finding
when the new code can silently return partial assets, lineage, or metadata.

Do not report this rule when the diff does not change source-result iteration.
