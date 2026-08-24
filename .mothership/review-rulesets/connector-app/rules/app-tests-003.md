---
schema: 2
id: APP-TESTS-003
level: L2
category: test-coverage
globs: []
severity: HIGH
suppressible: false
---
# Integration tier: real component, fake source

Integration tests exercise the app against REAL components with the
source faked: real DuckDB/parquet/filesystem, recorded HTTP at the
client seam, workflow logic in the test environment, config through the
REAL resolution path.

Flag the missing integration test — by name — when the diff:

- Adds a config flag or branching path: unit tests that mock the config
  reader prove nothing about the path production takes (env layering has
  silently overridden explicit inputs before). One integration test per
  branch, through real resolution — an on/off path counts as covered
  only when BOTH branches execute for real.
- Changes the client seam (pagination, retries, auth refresh): recorded
  responses must include the awkward pages — empty page with a
  next-page token, rate-limit mid-sequence.
- Changes anything an embedded engine executes (SQL dialect, parquet
  schema handling): the real engine must run it — unit-level string
  assertions cannot catch dialect drift.
- Changes cross-activity data handoffs: the test writes and reads
  through the real storage seam, not an in-memory stand-in.

State the suggestion concretely: "this needs an integration test that
<does X> because unit coverage stops at <seam>."
