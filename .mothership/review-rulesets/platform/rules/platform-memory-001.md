---
schema: 2
id: PLATFORM-MEMORY-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Bounded memory on unbounded sources

- Stream unbounded result sets and write per batch — `list(generator)`,
  `.fetchall()`, or whole-entity loads on unbounded data are forbidden
  unless a bounded-size argument is documented at the call site.
- Every cache justifies a documented memory bound; an unbounded cache on a
  per-asset key grows with tenant size.
- Embedded engines (DuckDB and similar) are not cgroup-aware: they size to
  host RAM and are OOM-killed before they spill. MUST set an explicit
  memory limit AND a spill/temp directory on every connection.
- FLAG unbounded `concat` over shard sets and O(N^2) re-serialization per
  row — known failure shapes on skewed inputs.
