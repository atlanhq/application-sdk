---
schema: 2
id: APP-STORAGE-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Cross-activity data goes through the object store

- MUST pass data between activities via the object store under the task's
  `output_path` — activities land on different pods; local disk and PVCs
  are per-pod.
- MUST NOT read a local file another activity produced ("local-first"):
  a pod that also ran the producer holds a partial slice on disk.
- MUST delete local files after successful upload — retained shards fill
  the pod filesystem and poison later local reads.
- Producers MUST clear their declared outputs before each attempt —
  platform retries write to the same prefix, and a dead attempt's shards
  otherwise get declared by the next one.
- A persisted progress marker (watermark) has exactly one owner; two loops
  sharing one marker with separate freeze flags silently lose data.
- MUST NOT use module-level mutable state to pass data between tasks, or
  write outside the task's `output_path`.
