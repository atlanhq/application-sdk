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

- MUST stream unbounded result sets and write per batch — never materialise
  a full source extract in memory. Pod limits are fixed; tenant size is not.
- FLAG unbounded list accumulation where a generator would do, unbounded
  `concat` over shard sets, and O(N^2) re-serialisation per row.
- Embedded engines (DuckDB and similar) are not cgroup-aware: they size to
  host RAM and are OOM-killed before they spill. MUST set an explicit
  memory limit AND a spill/temp directory when using one.
